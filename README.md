# Neural Splines: Fitting 3D Surfaces with Inifinitely-Wide Neural Networks
![Neural Splines Teaser](https://github.com/fwilliams/neural-splines/blob/master/teaser.png)
This repository contains the official implementation of the CVPR 2021 (Oral) paper [Neural Splines: Fitting 3D Surfaces with Infinitely-Wide Neural Networks](https://arxiv.org/abs/2006.13782).

## System Requirements
Neural Splines uses [FALKON](https://arxiv.org/abs/1705.10958), a state-of-the-art kernel ridge regression solver to fit 
surfaces on one or more GPUs. We thus require at least one GPU to run Neural Splines. 
We also recommend running this code on a machine with a lot of memory if you want to reconstruct large point clouds 
since Neural Splines stores an MxM preconditioner matrix in CPU memory (where M is the number of Nystrom samples). 

## Installing Dependencies
Neural splines has several dependencies which must be installed before it can be used. Some of these dependencies must be built and take time to install. 
There are three ways to install dependencies:

#### With `conda`
Simply run
```
conda env create -f environment.yml
```
and then go grab a coffee ☕. When you get back, you will have a conda environment called `neural-splines` with the right dependencies installed.

#### With `pip`
We include several `requirement-*.txt` files in the `requirements` directory depending on your version of cuda. Choose the right file for your installation then run
```
pip install -r requirements/requirements-cuda<VERSION>.txt
```
and then go grab a coffee ☕.

#### Manually (not recommended)
You will need to install the following dependencies manually to use this repository:
* [PyTorch](https://pytorch.org/)
* [scikit-image](https://scikit-image.org/)
* [numpy](https://numpy.org/)

You will also need to build the following dependencies from source. The easiest way to do this is with `pip` (see commands below), but you can also clone the linked repositories and run `setup.py install`:
* [point-cloud-utils](https://github.com/fwilliams/point-cloud-utils/tree/neural-splines): `pip install git+https://github.com/fwilliams/point-cloud-utils.git@neural-splines`
* [FALKON](https://github.com/fwilliams/falkon/tree/kml): `pip install git+https://github.com/fwilliams/falkon.git@kml`
* [KeOps](https://github.com/fwilliams/keops/tree/falkon)`pip install git+https://github.com/fwilliams/keops.git@falkon`

## Usage Instructuctions
There are two scripts in this repository to fit surfaces:
* `fit.py` fits an input point cloud using a single Neural Spline
* `fit_grid.py` fits an input point cloud in chunks using a Neural Spline per chunk

### Running `fit.py`

`fit.py` fits an input point cloud using a single Neural Spline and requires at least the following arguments
```
usage: fit.py <input_point_cloud> <eps> <num_nystrom_samples> <grid-size>
```
where
* **`<input_point_cloud>`** is a path to a PLY file containing 3D points and corresponding normals
* **`<eps>`** is a spacing parameter used for finite difference approximation of the gradient term in the kernel. 
  To capture all surface details this should be less than half the smallest distance between two points. 
  Generally setting this to values smalelr than `0.5/grid_size` is reasonable for this parameter
* **`<num_nystrom_samples>`** is the number of points to use as basis centers. A larger number of Nystrom samples will yield 
  a more accurate reconstruction but increase runtime and CPU memory usage. Generally good values for this are between 
  `10*sqrt(N)` and `100*sqrt(N)` where `N` is the number of input points.
* **`<grid-size>`** is the number of voxel cells along the longest axis of the bounding box on which the reconstructed 
  function gets sampled. For example if `<grid_size>` is `128` and the bounding box of the input pointcloud has dimensions `[1, 0.5, 0.5]`, then we will sample the function on a `128x64x64` voxel grid before extracting a mesh.
  
Additionally, `fit.py` accepts the following optional arguments which can alter the behavior and performance of
the fitting process:
  * **`--scale <SCALE>`**: Reconstruct the surface in a bounding box whose diameter is --scale times bigger than the diameter of the bounding box of the input points. Defaults is 1.1.
  * **`--regularization <REGULARIZATION>`**: Regularization penalty for kernel ridge regression. Default is 1e-7.
  * **`--nystrom-mode <NYSTROM_MODE>`**: How to generate nystrom samples. Default is 'k-means'. Must be one of
    - 'random': choose Nyström samples at random from the input
    - 'blue-noise': downsample the input with blue noise to get Nyström samples
    - 'k-means': use k-means  clustering to generate Nyström samples
  * **`--voxel-downsample-threshold <VOXEL_DOWNSAMPLE_THRESHOLD>`**: If the number of input points is greater than this value, downsample it by averaging points and normals within voxels on a grid. The size of the voxel grid is determined via the --grid-size argument. Default is 150_000.NOTE: This can massively  speed up reconstruction for very large point clouds and generally won't throw away any details.
  * **`--kernel <KERNEL>`**: Which kernel to use. Must be one of 'neural-spline' or 'spherical-laplace'. Default is 'neural-spline'.NOTE: The spherical laplace is a good approximation to the neural tangent kernel (see [this paper](https://arxiv.org/pdf/2007.01580.pdf) for details)
  * **`--seed <SEED>`**: Random number generator seed to use.
  * **`--out <OUT>`**:  Path to file to save reconstructed mesh in.
  * **`--save-grid`**: If set, save the function evaluated on a voxel grid to {out}.grid.npy where out is the value of the --out argument.
  * **`--save-points`**: If set, save the tripled input points, their occupancies, and the Nyström samples to an npz file named {out}.pts.npz where out is the value of the --out argument.
  * **`--cg-max-iters <CG_MAX_ITERS>`**: Maximum number of conjugate gradient iterations. Default is 20.
  * **`--cg-stop-thresh <CG_STOP_THRESH>`**: Stop threshold for the conjugate gradient algorithm. Default is 1e-5.
  * **`--dtype DTYPE`**: Scalar type of the data. Must be one of 'float32' or 'float64'. Warning: float32 may not work very well for complicated inputs.
  * **`--outer-layer-variance <OUTER_LAYER_VARIANCE>`**: Variance of the outer layer of the neural network from which the neural spline kernel arises from. Default is 1.0.
  * **`--verbose`**: If set, spam your terminal with debug information

