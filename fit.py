import argparse

import numpy as np
import point_cloud_utils as pcu
import torch
from skimage.measure import marching_cubes

from common import load_point_cloud, scale_bounding_box_diameter, fit_cell, eval_cell, point_cloud_bounding_box


def reconstruct_on_voxel_grid(model, grid_width, scale, bbox_normalized, bbox_input, dtype=torch.float64):
    scaled_bbn_min, scaled_bbn_size = scale_bounding_box_diameter(bbox_normalized, scale)
    scaled_bbi_min, scaled_bbi_size = scale_bounding_box_diameter(bbox_input, scale)

    plt_range_min, plt_range_max = scaled_bbn_min, scaled_bbn_min + scaled_bbn_size
    grid_size = np.round(bbox_normalized[1] * grid_width).astype(np.int64)

    print(f"Evaluating reconstructed function on grid of size {grid_size[0]}x{grid_size[1]}x{grid_size[2]}...")
    xgrid = np.stack([_.ravel() for _ in np.mgrid[plt_range_min[0]:plt_range_max[0]:grid_size[0] * 1j,
                                                  plt_range_min[1]:plt_range_max[1]:grid_size[1] * 1j,
                                                  plt_range_min[2]:plt_range_max[2]:grid_size[2] * 1j]],
                     axis=-1)
    xgrid = torch.from_numpy(xgrid).to(dtype)
    xgrid = torch.cat([xgrid, torch.ones(xgrid.shape[0], 1).to(xgrid)], dim=-1).to(dtype)

    ygrid = model.predict(xgrid).reshape(grid_size[0], grid_size[1], grid_size[2])

    size_per_voxel = scaled_bbi_size / (grid_size - 1.0)

    v, f, n, vals = marching_cubes(ygrid.detach().cpu().numpy(), level=0.0, spacing=size_per_voxel)
    v += scaled_bbi_min

    return ygrid, (v.astype(np.float64), f.astype(np.int32), n.astype(np.float64), vals.astype(np.float64))


def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("input_point_cloud", type=str, help="Path to the input point cloud to reconstruct.")
    argparser.add_argument("eps", type=float,
                           help="Perturbation amount for finite differencing. To approximate the gradient of the "
                                "function, we sample points +/- eps along the normal direction. "
                                "A reasonable value for this is half the minimum distance between any two points.")
    argparser.add_argument("num_nystrom_samples", type=int, default=-1,
                           help="Number of Nyström samples to use for kernel ridge regression. "
                                "If negative, don't use Nyström sampling."
                                "This is the number of basis centers to use to represent the final function. "
                                "If this value is too small, the reconstruction can miss details in the input. "
                                "Values between 10-100 times sqrt(N) (where N = number of input points) are "
                                "generally good depending on the complexity of the input shape.")
    argparser.add_argument("grid_size", type=int,
                           help="When reconstructing the mesh, use this many voxels along the longest side of the "
                                "bounding box. Default is 128.")

    argparser.add_argument("--scale", type=float, default=1.1,
                           help="Reconstruct the surface in a bounding box whose diameter is --scale times bigger than"
                                " the diameter of the bounding box of the input points. Defaults is 1.1.")

    argparser.add_argument("--regularization", type=float, default=1e-7,
                           help="Regularization penalty for kernel ridge regression. Default is 1e-7.")
    argparser.add_argument("--nystrom-mode", type=str, default="k-means",
                           help="How to generate nystrom samples. Default is 'k-means'. Must be one of "
                                "(1) 'random': choose Nyström samples at random from the input, "
                                "(2) 'blue-noise': downsample the input with blue noise to get Nyström samples, or "
                                "(3) 'k-means': use k-means clustering to generate Nyström samples. "
                                "Default is 'k-means'")
    argparser.add_argument("--voxel-downsample-threshold", type=int, default=150_000,
                           help="If the number of input points is greater than this value, downsample it by "
                                "averaging points and normals within voxels on a grid. The size of the voxel grid is "
                                "determined via the --grid-size argument. Default is 150_000."
                                "NOTE: This can massively speed up reconstruction for very large point clouds and "
                                "generally won't throw away any details.")
    argparser.add_argument("--kernel", type=str, default="neural-spline",
                           help="Which kernel to use. Must be one of 'neural-spline', 'spherical-laplace',"
                                " or 'linear-angle'. Default is 'neural-spline'."
                                "NOTE: The spherical laplace is a good approximation to the neural tangent kernel"
                                "(see https://arxiv.org/pdf/2007.01580.pdf for details)")
    argparser.add_argument("--seed", type=int, default=-1, help="Random number generator seed to use.")

    argparser.add_argument("--out", type=str, default="recon.ply", help="Path to file to save reconstructed mesh in.")
    argparser.add_argument("--save-grid", action="store_true",
                           help="If set, save the function evaluated on a voxel grid to {out}.grid.npy "
                                "where out is the value of the --out argument.")
    argparser.add_argument("--save-points", action="store_true",
                           help="If set, save the tripled input points, their occupancies, and the Nyström samples "
                                "to an npz file named {out}.pts.npz where out is the value of the --out argument.")

    argparser.add_argument("--cg-max-iters", type=int, default=20,
                           help="Maximum number of conjugate gradient iterations. Default is 20.")
    argparser.add_argument("--cg-stop-thresh", type=float, default=1e-5,
                           help="Stop threshold for the conjugate gradient algorithm. Default is 1e-5.")

    argparser.add_argument("--dtype", type=str, default="float64",
                           help="Scalar type of the data. Must be one of 'float32' or 'float64'. "
                                "Warning: float32 may not work very well for complicated inputs.")
    argparser.add_argument("--outer-layer-variance", type=float, default=1.0,
                           help="Variance of the outer layer of the neural network from which the neural "
                                "spline kernel arises from. Default is 1.0.")
    argparser.add_argument("--verbose", action="store_true", help="Spam your terminal with debug information")
    args = argparser.parse_args()

    if args.dtype == "float64":
        dtype = torch.float64
    elif args.dtype == "float32":
        dtype = torch.float32
    else:
        raise ValueError(f"invalid --dtype argument. Must be one of 'float32' or 'float64' but got {args.dtype}")

    if args.seed > 0:
        seed = args.seed
    else:
        seed = np.random.randint(2 ** 32 - 1)
    print("Using seed", seed)
    torch.manual_seed(seed)
    np.random.seed(seed)

    x, n = load_point_cloud(args.input_point_cloud, dtype=dtype)

    scaled_bbox = point_cloud_bounding_box(x, args.scale)
    out_grid_size = torch.round(scaled_bbox[1] / scaled_bbox[1].max() * args.grid_size).to(torch.int32)
    voxel_size = scaled_bbox[1] / out_grid_size  # size of one voxel

    # Downsample points to grid resolution if there are enough points
    if x.shape[0] > args.voxel_downsample_threshold:
        print("Downsampling input point cloud to voxel resolution")
        x, n, _ = pcu.downsample_point_cloud_voxel_grid(voxel_size, x.numpy(), n.numpy(),
                                                        min_bound=scaled_bbox[0],
                                                        max_bound=scaled_bbox[0] + scaled_bbox[1])
        x, n = torch.from_numpy(x), torch.from_numpy(n)

    model, tx = fit_cell(x, n, scaled_bbox, seed, args)
    recon = eval_cell(model, scaled_bbox, tx, out_grid_size)
    v, f, n, c = marching_cubes(recon.numpy(), level=0.0, spacing=voxel_size)
    v += scaled_bbox[0].numpy() + 0.5 * voxel_size.numpy()

    pcu.write_ply(args.out, v.astype(np.float32), f.astype(np.int32), n.astype(np.float32), c.astype(np.float32))
    if args.save_grid:
        np.savez(args.out + ".grid", grid=recon.detach().cpu().numpy())

    if args.save_points:
        x_ny = model.ny_points_[:, :3] if model.ny_points_ is not None else None
        np.savez(args.out + ".pts",
                 x=x.detach().cpu().numpy(),
                 y=y.detach().cpu().numpy(),
                 x_ny=x_ny.detach().cpu().numpy())


if __name__ == "__main__":
    main()
