import argparse

import falkon
import numpy as np
import point_cloud_utils as pcu
import time
import torch
from skimage.measure import marching_cubes

from common.falkon_kernels import ArcCosineKernel, LaplaceKernelSphere, LaplaceKernelSphereDecay, DirectKernelSolver
from common import make_triples
import tqdm


def fit_model(x, y, penalty, num_ny, kernel_type="spherical-laplace", mode="falkon", maxiters=20, decay=-1.0,
              stop_thresh=1e-7, verbose=False):
    if isinstance(num_ny, torch.Tensor):
        selector = falkon.center_selection.FixedSelector(centers=num_ny, y_centers=None)
        num_ny = num_ny[0].shape[0]
        print("Using fixed Nystrom samples")
    else:
        selector = 'uniform'

    opts = falkon.FalkonOptions()
    opts.min_cuda_pc_size_64 = 1
    opts.cg_tolerance = stop_thresh
    # opts.cg_full_gradient_every = 10
    opts.debug = verbose
    opts.use_cpu = False
    opts.min_cuda_iter_size_64 = 1

    if kernel_type == "spherical-laplace":
        if decay > 0.0:
            print(f"Using Spherical Laplacian Kernel with decay = {decay}")
            kernel = LaplaceKernelSphereDecay(alpha=-1.0, gamma=0.5, sigma=decay, opt=opts)
        else:
            print("Using Spherical Laplacian Kernel")
            kernel = LaplaceKernelSphere(alpha=-1.0, gamma=0.5, opt=opts)
    elif kernel_type == "arccosine":
        if decay > 0.0:
            raise NotImplementedError("TODO: Implement arc cosine kernel with decay")
        else:
            print("Using Arccosine Kernel")
            kernel = ArcCosineKernel(opt=opts)
    else:
        raise ValueError("Invalid kernel_type")

    opts.debug = False

    fit_start_time = time.time()
    if mode == "falkon":
        model = falkon.Falkon(kernel=kernel, penalty=penalty, M=num_ny, options=opts, maxiter=maxiters,
                              center_selection=selector)
    elif mode == "direct":
        model = DirectKernelSolver(kernel=kernel, penalty=penalty)
    else:
        raise ValueError("'mode' must be one of 'direct' or 'falkon'")

    model.fit(x, y)
    print(f"Fit model in {time.time() - fit_start_time} seconds")
    return model


def eval_grid(model, grid_size, plot_range, nchunks=1):
    if isinstance(grid_size, float) or isinstance(grid_size, int):
        grid_size = [grid_size] * 3
    if isinstance(plot_range, float):
        prmin, prmax = [-plot_range] * 3, [plot_range] * 3
    else:
        prmin, prmax = plot_range
    print(f"Evaluating function on grid of size {grid_size[0]}x{grid_size[1]}x{grid_size[2]}...")
    xgrid = np.stack([_.ravel() for _ in np.mgrid[prmin[0]:prmax[0]:grid_size[0] * 1j,
                                                  prmin[1]:prmax[1]:grid_size[1] * 1j,
                                                  prmin[2]:prmax[2]:grid_size[2] * 1j]],
                     axis=-1)
    xgrid = torch.from_numpy(xgrid)
    xgrid = torch.cat([xgrid, torch.ones(xgrid.shape[0], 1).to(xgrid)], dim=-1).to(torch.float64)

    if nchunks > 1:
        ygrid = []
        chunk_size = int(np.ceil(xgrid.shape[0] / nchunks))
        for i in tqdm.tqdm(range(nchunks)):
            ibeg, iend = i * chunk_size, (i + 1) * chunk_size
            ygrid.append(model.predict(xgrid[ibeg:iend]))

        ygrid = torch.cat(ygrid).reshape(grid_size[0], grid_size[1], grid_size[2])
    else:
        ygrid = model.predict(xgrid).reshape(grid_size[0], grid_size[1], grid_size[2])
    return ygrid


def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("input_point_cloud", type=str, help="Path to the input point cloud to reconstruct.")
    argparser.add_argument("--out", type=str, default="recon.ply", help="Path to file to save reconstructed mesh in.")

    argparser.add_argument("--kernel", type=str, default="spherical-laplace",
                           help="Which kernel to use. Must be one of 'spherical-laplace' or 'arccosine'.")
    argparser.add_argument("--penalty", type=float, default=0.0,
                           help="Regularization penalty for kernel ridge regression.")
    argparser.add_argument("--num-ny", type=int, default=1024,
                           help="Number of Nyström samples for kernel ridge regression.")
    argparser.add_argument("--seed", type=int, default=-1, help="Random number generator seed to use.")

    argparser.add_argument("--padding", type=float, default=np.inf,
                           help="If set to a positive value, will normalize the input point cloud so that it is "
                                "centered in a bounding cube of shape [-l, l]^ where l = plot_range - padding. "
                                "Here plot_range is the --plot-range argument and padding is this argument.")
    argparser.add_argument("--plot-range", "-pr", type=float, default=1.0,
                           help="Domain on which to sample the reconstructed function when constructing the "
                                "output voxel grid. The reconstructed function gets sampled on [-pr, pr]^3 where pr is "
                                "the `--plot-range` argument.")
    argparser.add_argument("--grid-size", "-g", type=int, default=128,
                           help="Size G of the voxel grid to reconstruct on. I.e. we sample the reconstructed "
                                "function on a G x G x G voxel grid.")
    argparser.add_argument("--save-grid", action="store_true",
                           help="If set, save a .npy file with the function evaluated on a voxel grid of "
                                "shape GxGxG where G is the --grid-width argument")
    argparser.add_argument("--save-points", action="store_true", help="Save input points and Nystrom samples")

    argparser.add_argument("--mode", type=str, default="falkon",
                           help="Type of solver to use. Must be either 'falkon' for Falkon conjugate gradient or "
                                "'direct' to use a dense LU factorization")
    argparser.add_argument("--eps", type=float, default=0.01,
                           help="Amount to perturb input points around surface to construct occupancy point cloud. "
                                "A reasonable value for this is half the minimum distance between any two points.")
    argparser.add_argument("--lloyd-nystrom", action="store_true",
                           help="Generate Nystrom samples by doing lloyd relaxation on the input mesh")
    argparser.add_argument("--decay", type=float, default=-1.0,
                           help="If set to a positive value, modulate the kernel with the Gaussian kernel with "
                                "standard deviation equal to this argument.")
    argparser.add_argument("--cg-max-iters", type=int, default=20,
                           help="Maximum number of conjugate gradient iterations.")
    argparser.add_argument("--cg-stop-thresh", type=float, default=1e-2, help="Stop threshold for conjugate gradient")
    argparser.add_argument("--verbose", action="store_true", help="Spam your terminal with debug information")
    argparser.add_argument("--debug-plot", action="store_true", help="Plot the input and nystrom samples")
    argparser.add_argument("--debug-print-mem", action="store_true", help="Print memory stats")
    args = argparser.parse_args()

    if args.seed > 0:
        seed = args.seed
    else:
        seed = np.random.randint(2 ** 32 - 1)
    print("Using seed", seed)
    torch.manual_seed(seed)
    np.random.seed(seed)

    v, f, n, _ = pcu.read_ply(args.input_point_cloud, dtype=np.float64)
    mask = np.linalg.norm(n, axis=-1) > 1e-5
    if f.shape[0] > 0:
        bad_idxs = np.nonzero(~mask)
        bad_faces = np.isin(f[:, 0], bad_idxs) | np.isin(f[:, 1], bad_idxs) | np.isin(f[:, 2], bad_idxs)
        faces_mask = np.logical_not(bad_faces)
        f = f[faces_mask]
    n[mask] /= np.linalg.norm(n[mask], axis=-1, keepdims=True)

    if np.isfinite(args.padding):
        v -= v.min(0)[np.newaxis, :]
        bbox = v.max(0) - v.min(0)
        v -= bbox / 2.0
        v /= bbox.max()
        bbox = (v.max(0) - v.min(0))
        plot_range = v.min(0) - args.padding * bbox, v.max(0) + args.padding * bbox
        bbox = v.max(0) - v.min(0)
        grid_size = np.round(bbox * args.grid_size).astype(np.int64)
    else:
        plot_range = args.plot_range
        grid_size = args.grid_size

    x = torch.from_numpy(v[mask]).to(torch.float64)
    n = torch.from_numpy(n[mask]).to(torch.float64)
    x, y = make_triples(x, n, args.eps)

    if args.lloyd_nystrom:
        # FIXME: Lloyd
        # if f.shape[0] != 0:
        #     print(v.shape, f.max())
        #     x_ny = pcu.sample_mesh_lloyd(v, f, args.num_ny)
        #     x_ny = torch.from_numpy(x_ny).to(torch.float64)
        # else:
        seed = args.seed if args.seed > 0 else 0
        ny_idx = pcu.prune_point_cloud_poisson_disk(x.numpy(), args.num_ny, random_seed=seed)
        x_ny = x[ny_idx]
        x_ny = torch.cat([x_ny, torch.ones(x_ny.shape[0], 1).to(x_ny)], dim=-1).to(torch.float64)
        num_ny = x_ny
        ny_count = x_ny.shape[0]
    else:
        num_ny = args.num_ny if args.num_ny > 0 else v.shape[0]
        ny_count = num_ny

    yb = np.ascontiguousarray(np.array(y.cpu().numpy().astype(np.float64), order="C"))
    xb = torch.cat([x, torch.ones(x.shape[0], 1).to(x)], dim=-1).to(torch.float64)
    yb = torch.from_numpy(yb)
    perm = torch.randperm(xb.shape[0])
    xb, yb = xb[perm], yb[perm]

    if args.debug_plot:
        from mayavi import mlab
        mlab.figure()
        mlab.points3d(xb[:, 0], xb[:, 1], xb[:, 2], yb, scale_factor=0.005, scale_mode="none")

        if args.lloyd_nystrom:
            mlab.figure()
            if f.shape[0] > 0:
                mlab.triangular_mesh(v[:, 0], v[:, 1], v[:, 2], f, color=(0.45, 0.45, 0.45))
            else:
                mlab.points3d(v[:, 0], v[:, 1], v[:, 2], scale_factor=0.005, scale_mode="none")
            mlab.points3d(x_ny[:, 0], x_ny[:, 1], x_ny[:, 2],
                          scale_factor=0.01, color=(0.3, 0.3, 0.77), scale_mode="none")
        mlab.show()
    print(f"Fitting {xb.shape[0]} points using {ny_count} Nystrom samples")
    mdl = fit_model(xb, yb, args.penalty, num_ny, mode="falkon", maxiters=args.cg_max_iters,
                    kernel_type=args.kernel, decay=args.decay, stop_thresh=args.cg_stop_thresh,
                    verbose=args.verbose)
    if args.debug_print_mem:
        print("CUDA MEMORY SUMMARY")
        print(torch.cuda.memory_summary('cuda'))

    grid = eval_grid(mdl, grid_size=grid_size, plot_range=plot_range, nchunks=1)

    if isinstance(plot_range, tuple):
        plot_bb = plot_range[1] - plot_range[0]
        grid_spacing = plot_bb / (grid_size.astype(np.float64) - 1.0)
    else:
        grid_spacing = np.ones(3) * (0.577 * 2.0 / (grid_size.astype(np.float64) - 1.0))
    v, f, n, vals = marching_cubes(grid.detach().cpu().numpy(),
                                   level=0.0, spacing=grid_spacing)
    if isinstance(plot_range, tuple):
        v -= ((plot_range[1] - plot_range[0]) / 2.0)
    else:
        v -= plot_range
    pcu.write_ply(args.out, v, f, n.astype(v.dtype), vals.astype(v.dtype))
    if args.save_grid:
        np.save(args.out + ".grid", grid.detach().cpu().numpy())

    if args.save_points:
        torch.save((x, x_ny[:, :3], y), args.out + ".pts.pth")


if __name__ == "__main__":
    main()