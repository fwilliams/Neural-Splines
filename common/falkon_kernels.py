import functools
from abc import ABC
from abc import abstractmethod
from typing import Optional

import numpy as np
import torch

from falkon.kernels import Kernel, KeopsKernelMixin
from falkon.options import FalkonOptions
from falkon.sparse.sparse_tensor import SparseTensor


def extract_float(d):
    if isinstance(d, torch.Tensor):
        try:
            # tensor.item() works if tensor is a scalar, otherwise it throws
            # a value error.
            return d.item()
        except ValueError:
            raise ValueError("Item is not a scalar")
    else:
        try:
            return float(d)
        except TypeError:
            raise TypeError("Item must be a scalar or a tensor.")


def k_spherical_laplace(x, xp, gamma, alpha):
    x = x.unsqueeze(1)  # [s1, 1, d]
    xp = xp.unsqueeze(0)  # [1, s2, d]
    x_dot_xp = (x * xp).sum(-1)  # [s1, s2]
    norm_x = torch.norm(x, dim=-1)  # [s1, 1]
    norm_xp = torch.norm(xp, dim=-1)  # [1, s2]
    norm_x_xp = norm_x * norm_xp  # [s2, s1]

    cos_alpha_x = torch.clamp(x_dot_xp / norm_x_xp, -1.0, 1.0)  # [s1, s2]

    return norm_x_xp * torch.exp(alpha * torch.pow(1.0 - cos_alpha_x, gamma)).squeeze(-1)


def k_arccos(x, xp):
    # x and xp have shape [s1, d] and [s2, d] respectively

    x = x.unsqueeze(1)  # [s1, 1, d]
    xp = xp.unsqueeze(0)  # [1, s2, d]
    x_dot_xp = (x * xp).sum(-1)  # [s1, s2]
    norm_x = torch.norm(x, dim=-1)  # [s1, 1]
    norm_xp = torch.norm(xp, dim=-1)  # [1, s2]
    norm_x_xp = norm_x * norm_xp  # [s2, s1]

    cos_alpha_x = torch.clamp(x_dot_xp / norm_x_xp, -1.0, 1.0)  # [s1, s2]
    alpha_x = torch.acos(cos_alpha_x)  # [s1, s2]
    sin_alpha_x = torch.sin(alpha_x)  # [s2, s1]

    K = norm_x_xp * (sin_alpha_x + (np.pi - alpha_x) * cos_alpha_x)  # [s1, s2]
    return K / np.pi


def k_ntk(x, xp, variance=1.0):
    # x and xp have shape [s1, d] and [s2, d] respectively

    x = x.unsqueeze(1)  # [s1, 1, d]
    xp = xp.unsqueeze(0)  # [1, s2, d]
    x_dot_xp = (x * xp).sum(-1)  # [s1, s2]
    norm_x = torch.norm(x, dim=-1)  # [s1, 1]
    norm_xp = torch.norm(xp, dim=-1)  # [1, s2]
    norm_x_xp = norm_x * norm_xp  # [s2, s1]

    cos_alpha_x = torch.clamp(x_dot_xp / norm_x_xp, -1.0, 1.0)  # [s1, s2]
    alpha_x = torch.acos(cos_alpha_x)  # [s1, s2]
    sin_alpha_x = torch.sin(alpha_x)  # [s2, s1]

    K = norm_x_xp * (sin_alpha_x + (1.0 + variance) * (np.pi - alpha_x) * cos_alpha_x)  # [s1, s2]
    return K / np.pi


class DirectKernelSolver:
    def __init__(self, kernel, penalty):
        self.penalty = penalty
        self.alpha_ = None
        self.x_ = None
        self.kernel = kernel

    def fit(self, x, y):
        Kxx = self.kernel.direct_kernel(x, x)
        self.x_ = x
        self.alpha_ = torch.inverse(Kxx + self.penalty * torch.eye(Kxx.shape[0], Kxx.shape[1])) @ y

    def predict(self, x):
        Kxxp = self.kernel.direct_kernel(x, self.x_)
        return Kxxp @ self.alpha_


class DirectKernelMixin:
    @abstractmethod
    def direct_kernel(self, X1, X2):
        pass


class ArcCosineKernel(Kernel, KeopsKernelMixin, ABC, DirectKernelMixin):
    kernel_type = "dot-product"

    def __init__(self, opt: Optional[FalkonOptions] = None):
        super().__init__("ArcCosine", self.kernel_type, opt)
        self.debug = opt.debug if opt is not None else False
        # self.alpha = torch.tensor(extract_float(alpha), dtype=torch.float64)

    def _keops_mmv_impl(self, X1, X2, v, kernel, out, opt):
        if self.debug:
            print(f"ArcCosineKernel._keops_mmv_impl(X1={X1.shape}, X2={X2.shape}, v, kernel, out, opt)")

        norm_xy = '(Norm2(X) * Norm2(Y))'
        cos_theta = '((Normalize(X) | Normalize(Y)))'
        theta = 'Acos(' + cos_theta + ')'

        j1 = '(Sin({theta}) + (pi - {theta}) * {cos_theta})'.format(theta=theta, cos_theta=cos_theta)
        formula = '(({norm_xy} / pi) * {j1}) * v'.format(norm_xy=norm_xy, j1=j1)
        aliases = [
            'X = Vi(%d)' % (X1.shape[1]),
            'Y = Vj(%d)' % (X2.shape[1]),
            'v = Vj(%d)' % (v.shape[1]),
            'pi = Pm(1)',
        ]
        other_vars = [torch.tensor([np.pi]).to(dtype=X1.dtype, device=X1.device)]

        return self.keops_mmv(X1, X2, v, out, formula, aliases, other_vars, opt)

    def _decide_mmv_impl(self, X1, X2, v, opt):
        if self.keops_can_handle_mmv(X1, X2, v, opt):
            return self._keops_mmv_impl
        else:
            return super()._decide_mmv_impl(X1, X2, v, opt)

    def _decide_dmmv_impl(self, X1, X2, v, w, opt):
        if self.keops_can_handle_dmmv(X1, X2, v, w, opt):
            return functools.partial(self.keops_dmmv_helper, mmv_fn=self._keops_mmv_impl)
        else:
            return super()._decide_dmmv_impl(X1, X2, v, w, opt)

    def _prepare(self, X1, X2, **kwargs):
        if self.debug:
            print(f"ArcCosineKernel._prepare(X1={X1.shape}, X2={X2.shape}, *kwargs)")
        return [torch.norm(X1, dim=-1).unsqueeze(1), torch.norm(X2, dim=-1).unsqueeze(0)]

    def _prepare_sparse(self, X1: SparseTensor, X2: SparseTensor):
        raise NotImplementedError("ArcCosineKernel does not implement sparse prepare")

    def _apply(self, X1: torch.Tensor, X2: torch.Tensor, out: torch.Tensor):
        out.addmm_(X1, X2)

    def _apply_sparse(self, X1: SparseTensor, X2: SparseTensor, out: torch.Tensor):
        raise NotImplementedError("ArcCosineKernel does not implement sparse apply")

    def _finalize(self, A: torch.Tensor, d):
        if self.debug:
            print(f"ArcCosineKernel._finalize(A={A.shape}, d)")
        n1, n2 = [_.to(A) for _ in d]
        A.div_(n1)
        A.div_(n2)
        A.clamp_(-1.0, 1.0)  # cos(theta)

        B = torch.acos(A)  # theta
        C = torch.sin(B)  # sin(theta)
        B.mul_(-1.0)  # -theta
        B.add_(np.pi)  # pi - theta
        A.mul_(B)  # (pi - theta) * cos(theta)
        A.add_(C)  # sin(theta) + (pi - theta) * cos(theta) = J_1
        del B, C

        A.mul_(n1)  # |X| * J_1
        A.mul_(n2)  # |X| * |Y| * J_1
        A.div_(np.pi)  # |X| * |Y| * J_1 / pi
        return A

    def direct_kernel(self, X1, X2):
        return k_arccos(X1, X2)

    def __str__(self):
        return f"ArcCosineKernel()"

    def __repr__(self):
        return self.__str__()


class NeuralTangentKernel(Kernel, KeopsKernelMixin, ABC, DirectKernelMixin):
    kernel_type = "angle"

    def __init__(self, variance: float = 1.0, opt: Optional[FalkonOptions] = None):
        super().__init__("NeuralTangentKernel", self.kernel_type, opt)
        self.debug = opt.debug if opt is not None else False
        self.variance = variance

    def _keops_mmv_impl(self, X1, X2, v, kernel, out, opt):
        if self.debug:
            print(f"NeuralTangentKernel._keops_mmv_impl(X1={X1.shape}, X2={X2.shape}, v, kernel, out, opt)")

        theta = 'two * Atan2(Norm2(Norm2(Y) * X - Norm2(X) * Y), Norm2(Norm2(Y) * X + Norm2(X) * Y))'
        norm_xy = '(Norm2(X) * Norm2(Y))'

        j01 = f'({norm_xy} * (Sin({theta}) + (one + variance) * (pi - {theta}) * Cos({theta})))'
        formula = f'({j01} / pi) * v'
        aliases = [
            'X = Vi(%d)' % (X1.shape[1]),
            'Y = Vj(%d)' % (X2.shape[1]),
            'v = Vj(%d)' % (v.shape[1]),
            'pi = Pm(1)',
            'variance = Pm(1)',
            'one = Pm(1)',
            'two = Pm(1)'
        ]
        other_vars = [torch.tensor([np.pi]).to(dtype=X1.dtype, device=X1.device),
                      torch.tensor([self.variance]).to(dtype=X1.dtype, device=X1.device),
                      torch.tensor([1.0]).to(dtype=X1.dtype, device=X1.device),
                      torch.tensor([2.0]).to(dtype=X1.dtype, device=X1.device)]

        return self.keops_mmv(X1, X2, v, out, formula, aliases, other_vars, opt)

    def _decide_mmv_impl(self, X1, X2, v, opt):
        if self.keops_can_handle_mmv(X1, X2, v, opt):
            return self._keops_mmv_impl
        else:
            return super()._decide_mmv_impl(X1, X2, v, opt)

    def _decide_dmmv_impl(self, X1, X2, v, w, opt):
        if self.keops_can_handle_dmmv(X1, X2, v, w, opt):
            return functools.partial(self.keops_dmmv_helper, mmv_fn=self._keops_mmv_impl)
        else:
            return super()._decide_dmmv_impl(X1, X2, v, w, opt)

    def _prepare(self, X1, X2, **kwargs):
        if self.debug:
            print(f"NeuralTangentKernel._prepare(X1={X1.shape}, X2={X2.shape}, *kwargs)")
        return []

    def _prepare_sparse(self, X1: SparseTensor, X2: SparseTensor):
        raise NotImplementedError("NeuralTangentKernel does not implement sparse prepare")

    def _apply(self, X1: torch.Tensor, X2: torch.Tensor, out: torch.Tensor):
        if self.debug:
            print(f"NeuralTangentKernel._apply(X1={X1.shape}, X2={X2.shape}, out={out.shape})")
        import cupy as cp
        from torch.utils.dlpack import to_dlpack
        from torch.utils.dlpack import from_dlpack

        kernel_code = r'''
        #define PI (DTYPE) (3.1415926535897932384626433832795028841971693993751058209749445923078164062)
        #define ONE (DTYPE) (1.0)
        extern "C" __global__
        void stable_kernel(const DTYPE* x1, const DTYPE* x2, DTYPE* out, const double variance, const int N, int M, int D) {
            const int I = (blockIdx.x * blockDim.x) + threadIdx.x;
            const int J = (blockIdx.y * blockDim.y) + threadIdx.y;

            if (I >= N || J >= M) {
                return;
            }

            DTYPE norm_x = (DTYPE) 0.0; //normf(D, &x1[I*D]);
            DTYPE norm_y = (DTYPE) 0.0; //normf(D, &x2[J*D]);

            #pragma unroll 
            for (int k = 0; k < D; k += 1) {
                norm_x = fma(x1[I * D + k], x1[I * D + k], norm_x);
                norm_y = fma(x2[J * D + k], x2[J * D + k], norm_y);
            }
            norm_x = sqrt(norm_x);
            norm_y = sqrt(norm_y);

            DTYPE arg1 = (DTYPE) 0.0;
            DTYPE arg2 = (DTYPE) 0.0;

            #pragma unroll
            for (int k = 0; k < D; k += 1) {
                DTYPE x1_ik = x1[I * D + k];
                DTYPE x2_jk = x2[J * D + k];
                DTYPE a1 = norm_y * x1_ik - norm_x * x2_jk;
                DTYPE a2 = norm_y * x1_ik + norm_x * x2_jk;

                arg1 = fma(a1, a1, arg1);
                arg2 = fma(a2, a2, arg2);
            }
            arg1 = sqrt(arg1);
            arg2 = sqrt(arg2);

            DTYPE angle = 2.0 * atan2(arg1, arg2);

            DTYPE norm_xy = norm_x * norm_y;
            DTYPE cos_angle = cos(angle);
            DTYPE sin_angle = sin(angle);
            DTYPE opv = ONE + (DTYPE)(variance);
            DTYPE K = norm_xy * (sin_angle + opv * (PI - angle) * cos_angle) / PI;
            out[I * M + J] = K;
        }
        '''
        str_dtype = "float" if X1.dtype == torch.float32 else "double"
        kernel_code = kernel_code.replace("DTYPE", str_dtype)
        kernel = cp.RawKernel(kernel_code, 'stable_kernel')

        X2 = X2.T.contiguous()  # This is passed in transposed... ugh

        # Convert X1 and X2 to CuPy arrays.
        x1cp = cp.fromDlpack(to_dlpack(X1))
        x2cp = cp.fromDlpack(to_dlpack(X2))
        outcp = cp.fromDlpack(to_dlpack(out))

        pt_dim = X1.shape[1]
        dims = X1.shape[0], X2.shape[0]
        threads = (16, 16)  # TODO: Maybe hardcoding this is bad
        blocks = tuple((dims[i] + threads[i] - 1) // threads[i] for i in range(2))

        print(x1cp.shape, x2cp.shape, outcp.shape)
        print(dims[0], dims[1], pt_dim)

        kernel(threads, blocks, (x1cp, x2cp, outcp, float(self.variance), dims[0], dims[1], pt_dim))
        out = from_dlpack(outcp.toDlpack())

    def _apply_sparse(self, X1: SparseTensor, X2: SparseTensor, out: torch.Tensor):
        raise NotImplementedError("NeuralTangentKernel does not implement sparse apply")

    def _finalize(self, A: torch.Tensor, d):
        if self.debug:
            print(f"NeuralTangentKernel._finalize(A={A.shape}, d)")
        return A

    def direct_kernel(self, X1, X2):
        return k_arccos(X1, X2)

    def __str__(self):
        return f"ArcCosineKernel()"

    def __repr__(self):
        return self.__str__()


class LaplaceKernelSphere(Kernel, KeopsKernelMixin, DirectKernelMixin, ABC):
    kernel_type = "dot-product"

    def __init__(self, alpha, gamma, opt: Optional[FalkonOptions] = None):
        super().__init__("LaplaceKernelSphere", self.kernel_type, opt)
        self.debug = opt.debug if opt is not None else False
        self.alpha = torch.tensor(extract_float(alpha), dtype=torch.float64)
        self.gamma = torch.tensor(extract_float(gamma), dtype=torch.float64)

    def _keops_mmv_impl(self, X1, X2, v, kernel, out, opt):
        if self.debug:
            print("LaplaceKernelSphere._keops_mmv_impl(X1, X2, v, kernel, out, opt)")
        # formula = 'Norm2(X) * Norm2(Y) * Exp(alpha * Sqrt(one - Clamp11(Normalize(X) | Normalize(Y)))) * v'
        formula = 'Norm2(X) * Norm2(Y) * Exp(alpha * Powf(one - Clamp11((Normalize(X) | Normalize(Y))), gamma)) * v'
        aliases = [
            'X = Vi(%d)' % (X1.shape[1]),
            'Y = Vj(%d)' % (X2.shape[1]),
            'v = Vj(%d)' % (v.shape[1]),
            'alpha = Pm(1)',
            'gamma = Pm(1)',
            'one = Pm(1)',
        ]
        other_vars = [torch.tensor([self.alpha]).to(dtype=X1.dtype, device=X1.device),
                      torch.tensor([self.gamma]).to(dtype=X1.dtype, device=X1.device),
                      torch.tensor([1.0]).to(dtype=X1.dtype, device=X1.device)]

        return self.keops_mmv(X1, X2, v, out, formula, aliases, other_vars, opt)

    def _decide_mmv_impl(self, X1, X2, v, opt):
        if self.keops_can_handle_mmv(X1, X2, v, opt):
            return self._keops_mmv_impl
        else:
            return super()._decide_mmv_impl(X1, X2, v, opt)

    def _decide_dmmv_impl(self, X1, X2, v, w, opt):
        if self.keops_can_handle_dmmv(X1, X2, v, w, opt):
            return functools.partial(self.keops_dmmv_helper, mmv_fn=self._keops_mmv_impl)
        else:
            return super()._decide_dmmv_impl(X1, X2, v, w, opt)

    def _prepare(self, X1, X2, **kwargs):
        if self.debug:
            print("LaplaceKernelSphere._prepare(X1, X2, *kwargs)")
        return [torch.norm(X1, dim=-1).unsqueeze(1), torch.norm(X2, dim=-1).unsqueeze(0)]

    def _prepare_sparse(self, X1: SparseTensor, X2: SparseTensor):
        raise NotImplementedError("LaplaceKernelSphere does not implement sparse prepare")

    def _apply(self, X1: torch.Tensor, X2: torch.Tensor, out: torch.Tensor):
        if self.debug:
            print("LaplaceKernelSphere._apply(X1, X2, out)")
        out.addmm_(X1, X2)

    def _apply_sparse(self, X1: SparseTensor, X2: SparseTensor, out: torch.Tensor):
        raise NotImplementedError("LaplaceKernelSphere does not implement sparse apply")

    def _finalize(self, A, d):
        if self.debug:
            print("LaplaceKernelSphere._finalize(A, d)")
        alpha = self.alpha.to(A)
        gamma = self.gamma.to(A)
        n1, n2 = [_.to(A) for _ in d]
        A.div_(n1)
        A.div_(n2)
        A.clamp_(-1.0, 1.0)
        A.mul_(-1.0)
        A.add_(1.0)
        A.pow_(gamma)
        A.mul_(alpha)
        A.exp_()
        A.mul_(n1)
        A.mul_(n2)
        return A

    def direct_kernel(self, X1, X2):
        return k_spherical_laplace(X1, X2, self.gamma, self.alpha)

    def __str__(self):
        return f"LaplaceKernelSphere(alpha={self.alpha})"

    def __repr__(self):
        return self.__str__()