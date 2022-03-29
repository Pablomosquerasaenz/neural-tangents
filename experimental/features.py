from typing import Optional, Callable, Sequence, Tuple
from jax import random
from jax import numpy as np
from jax.numpy.linalg import cholesky
import jax.example_libraries.stax as ostax

from neural_tangents import stax
from neural_tangents._src.utils import dataclasses
from neural_tangents._src.stax.linear import _pool_kernel, Padding, _get_dimension_numbers
from neural_tangents._src.stax.linear import _Pooling as Pooling

from experimental.sketching import TensorSRHT, PolyTensorSketch
from experimental.poly_fitting import kappa0_coeffs, kappa1_coeffs, kappa0, kappa1, relu_ntk_coeffs
""" Implementation for NTK Sketching and Random Features """


@dataclasses.dataclass
class Features:
  nngp_feat: np.ndarray
  ntk_feat: np.ndarray
  norms: np.ndarray

  batch_axis: int = 0
  channel_axis: int = -1

  replace = ...  # type: Callable[..., 'Features']


def layer(layer_fn):

  def new_layer_fns(*args, **kwargs):
    init_fn, feature_fn = layer_fn(*args, **kwargs)
    init_fn = _preprocess_init_fn(init_fn)
    feature_fn = _preprocess_feature_fn(feature_fn)
    return init_fn, feature_fn

  return new_layer_fns


def _preprocess_init_fn(init_fn):

  def init_fn_any(rng, input_shape_any, **kwargs):
    if _is_sinlge_shape(input_shape_any):
      input_shape = (input_shape_any, (-1, 0))
      return init_fn(rng, input_shape, **kwargs)
    else:
      return init_fn(rng, input_shape_any, **kwargs)

  return init_fn_any


def _is_sinlge_shape(input_shape):
  if len(input_shape) == 2:
    if all(isinstance(n, int) for n in input_shape):
      return True
    elif all(_is_sinlge_shape(s) for s in input_shape):
      return False
  elif len(input_shape) == 3:
    return _is_sinlge_shape(input_shape[:2])
  raise ValueError(input_shape)


# For flexible `feature_fn` with both input `np.ndarray` and with `Feature`.
# Followed https://github.com/google/neural-tangents/blob/main/neural_tangents/_src/stax/requirements.py
def _preprocess_feature_fn(feature_fn):

  def feature_fn_feature(feature, input, **kwargs):
    return feature_fn(feature, input, **kwargs)

  def feature_fn_x(x, input, **kwargs):
    feature = _inputs_to_features(x, **kwargs)
    return feature_fn(feature, input, **kwargs)

  def feature_fn_any(x_or_feature, input=None, **kwargs):
    if isinstance(x_or_feature, Features):
      return feature_fn_feature(x_or_feature, input, **kwargs)
    return feature_fn_x(x_or_feature, input, **kwargs)

  return feature_fn_any


def _inputs_to_features(x: np.ndarray,
                        batch_axis: int = 0,
                        channel_axis: int = -1,
                        **kwargs) -> Features:
  """Transforms (batches of) inputs to a `Features`."""

  # Followed the same initialization of Neural Tangents library.
  nngp_feat = x / x.shape[channel_axis]**0.5
  norms = np.linalg.norm(nngp_feat, axis=channel_axis)
  norms = np.expand_dims(np.where(norms > 0, norms, 1.0), channel_axis)
  nngp_feat = nngp_feat / norms
  ntk_feat = np.zeros((), dtype=nngp_feat.dtype)

  return Features(nngp_feat=nngp_feat,
                  ntk_feat=ntk_feat,
                  norms=norms,
                  batch_axis=batch_axis,
                  channel_axis=channel_axis)  # pytype:disable=wrong-keyword-args


# Modified the serial process of feature map blocks.
# Followed https://github.com/google/neural-tangents/blob/main/neural_tangents/stax.py
@layer
def serial(*layers):

  init_fns, feature_fns = zip(*layers)
  init_fn, _ = ostax.serial(*zip(init_fns, init_fns))

  def feature_fn(k, inputs, **kwargs):
    for f, input_ in zip(feature_fns, inputs):
      k = f(k, input_, **kwargs)
    k = _renormalize_feature(k)
    return k

  return init_fn, feature_fn


def _renormalize_feature(f: Features, **kwargs):
  nngp_feat = f.nngp_feat * f.norms
  ntk_feat = f.ntk_feat * f.norms
  return f.replace(nngp_feat=nngp_feat, ntk_feat=ntk_feat)


@layer
def DenseFeatures(out_dim: int,
                  W_std: float = 1.,
                  b_std: Optional[float] = None,
                  parameterization: str = 'ntk',
                  batch_axis: int = 0,
                  channel_axis: int = -1):

  if b_std is not None:
    raise NotImplementedError('Bias variable b_std is not implemented yet .'
                              ' Please set b_std to be `0`.')

  if parameterization != 'ntk':
    raise NotImplementedError(f'Parameterization ({parameterization}) is '
                              ' not implemented yet.')

  def init_fn(rng, input_shape):
    nngp_feat_shape, ntk_feat_shape = input_shape[0], input_shape[1]
    new_ntk_feat_shape = nngp_feat_shape[:-1] + (nngp_feat_shape[-1] +
                                                 ntk_feat_shape[-1],)

    if len(input_shape) > 2:
      return (nngp_feat_shape, new_ntk_feat_shape, input_shape[2] + 'D'), ()
    else:
      return (nngp_feat_shape, new_ntk_feat_shape, 'D'), ()

  def feature_fn(f: Features, input, **kwargs):
    nngp_feat : np.ndarray = f.nngp_feat
    ntk_feat : np.ndarray = f.ntk_feat
    norms : np.ndarray = f.norms

    norms *= W_std

    if ntk_feat.ndim == 0:  # check if ntk_feat is empty
      ntk_feat = nngp_feat
    else:
      ntk_feat = np.concatenate((ntk_feat, nngp_feat), axis=channel_axis)

    return f.replace(nngp_feat=nngp_feat, ntk_feat=ntk_feat, norms=norms)

  return init_fn, feature_fn


@layer
def ReluFeatures(feature_dim0: int = 1,
                 feature_dim1: int = 1,
                 sketch_dim: int = 1,
                 poly_degree: int = 8,
                 poly_sketch_dim: int = 1,
                 method: str = 'rf',
                 top_layer: bool = False):

  method = method.lower()
  assert method in ['rf', 'ps', 'exact', 'psrf', 'poly']

  def init_fn(rng, input_shape):
    nngp_feat_shape, ntk_feat_shape = input_shape[0], input_shape[1]
    new_nngp_feat_shape = nngp_feat_shape[:-1] + (feature_dim1,)
    new_ntk_feat_shape = ntk_feat_shape[:-1] + (sketch_dim,)
    net_shape = input_shape[2]
    layer_count = len(net_shape) // 2 + 1

    if method == 'rf':
      rng1, rng2, rng3 = random.split(rng, 3)
      # Random vectors for random features of arc-cosine kernel of order 0.
      W0 = random.normal(rng1, (nngp_feat_shape[-1], feature_dim0))
      # Random vectors for random features of arc-cosine kernel of order 1.
      W1 = random.normal(rng2, (nngp_feat_shape[-1], feature_dim1))
      # TensorSRHT of degree 2 for approximating tensor product.
      tensorsrht = TensorSRHT(rng=rng3,
                              input_dim1=ntk_feat_shape[-1],
                              input_dim2=feature_dim0,
                              sketch_dim=sketch_dim).init_sketches()  # pytype:disable=wrong-keyword-args

      return (new_nngp_feat_shape, new_ntk_feat_shape,
              net_shape + 'R'), (W0, W1, tensorsrht)

    elif method == 'ps':
      new_nngp_feat_shape = nngp_feat_shape[:-1] + (poly_sketch_dim,)
      rng1, rng2, rng3 = random.split(rng, 3)

      kappa1_coeff = kappa1_coeffs(poly_degree, layer_count - 1)
      kappa0_coeff = kappa0_coeffs(poly_degree, layer_count - 1)

      # PolySketch expansion for nngp features.
      polysketch = PolyTensorSketch(rng=rng1,
                                    input_dim=nngp_feat_shape[-1] //
                                    (1 + (layer_count > 1)),
                                    sketch_dim=poly_sketch_dim,
                                    degree=poly_degree).init_sketches()  # pytype:disable=wrong-keyword-args

      # TensorSRHT of degree 2 for approximating tensor product.
      tensorsrht = TensorSRHT(
          input_dim1=ntk_feat_shape[-1] // (1 + (layer_count > 1)),
          input_dim2=poly_degree * (polysketch.sketch_dim // 4 - 1) + 1,
          sketch_dim=sketch_dim,
          rng=rng2).init_sketches()  # pytype:disable=wrong-keyword-args

      return (new_nngp_feat_shape, new_ntk_feat_shape,
              net_shape + 'R'), (polysketch, tensorsrht,
                                 (kappa0_coeff, kappa1_coeff, layer_count))

    elif method == 'psrf':
      new_nngp_feat_shape = nngp_feat_shape[:-1] + (poly_sketch_dim,)
      rng1, rng2, rng3 = random.split(rng, 3)

      kappa1_coeff = kappa1_coeffs(poly_degree, layer_count - 1)

      # PolySketch expansion for nngp features.
      polysketch = PolyTensorSketch(rng=rng1,
                                    input_dim=nngp_feat_shape[-1] //
                                    (1 + (layer_count > 1)),
                                    sketch_dim=poly_sketch_dim,
                                    degree=poly_degree).init_sketches()  # pytype:disable=wrong-keyword-args

      # TensorSRHT of degree 2 for approximating tensor product.
      tensorsrht = TensorSRHT(rng=rng2,
                              input_dim1=ntk_feat_shape[-1] //
                              (1 + (layer_count > 1)),
                              input_dim2=feature_dim0,
                              sketch_dim=sketch_dim).init_sketches()  # pytype:disable=wrong-keyword-args

      # Random vectors for random features of arc-cosine kernel of order 0.
      if layer_count == 1:
        W0 = random.normal(rng3, (2 * nngp_feat_shape[-1], feature_dim0 // 2))
      else:
        W0 = random.normal(rng3, (nngp_feat_shape[-1], feature_dim0 // 2))

      return (new_nngp_feat_shape, new_ntk_feat_shape,
              net_shape + 'R'), (W0, polysketch, tensorsrht, (kappa1_coeff,
                                                              layer_count))

    elif method == 'poly':
      # This only uses the polynomial approximation without sketching.
      new_nngp_feat_shape = nngp_feat_shape[:-1] + (_prod(
          nngp_feat_shape[:-1]),)
      new_ntk_feat_shape = ntk_feat_shape[:-1] + (_prod(ntk_feat_shape[:-1]),)

      kappa1_coeff = kappa1_coeffs(poly_degree, layer_count - 1)
      kappa0_coeff = kappa0_coeffs(poly_degree, layer_count - 1)

      return (new_nngp_feat_shape, new_ntk_feat_shape,
              net_shape + 'R'), (kappa0_coeff, kappa1_coeff, layer_count)

    elif method == 'exact':
      # The exact feature map computation is for debug.
      new_nngp_feat_shape = nngp_feat_shape[:-1] + (_prod(
          nngp_feat_shape[:-1]),)
      new_ntk_feat_shape = ntk_feat_shape[:-1] + (_prod(ntk_feat_shape[:-1]),)

      return (new_nngp_feat_shape, new_ntk_feat_shape, net_shape + 'R'), ()

    else:
      raise NotImplementedError(f'Invalid method name: {method}')

  def feature_fn(f: Features, input=None, **kwargs) -> Features:

    input_shape: tuple = f.nngp_feat.shape[:-1]
    nngp_feat_dim: tuple = f.nngp_feat.shape[-1]
    ntk_feat_dim: tuple = f.ntk_feat.shape[-1]

    nngp_feat_2d: np.ndarray = f.nngp_feat.reshape(-1, nngp_feat_dim)
    ntk_feat_2d: np.ndarray = f.ntk_feat.reshape(-1, ntk_feat_dim)
    norms: np.ndarray = f.norms

    if method == 'rf':  # Random Features approach.
      W0: np.ndarray = input[0]
      W1: np.ndarray = input[1]
      tensorsrht: TensorSRHT = input[2]

      kappa0_feat = (nngp_feat_2d @ W0 > 0) / W0.shape[-1]**0.5
      del W0
      nngp_feat = (np.maximum(nngp_feat_2d @ W1, 0) /
                   W1.shape[-1]**0.5).reshape(input_shape + (-1,))
      del W1
      ntk_feat = tensorsrht.sketch(ntk_feat_2d, kappa0_feat,
                                   real_output=True).reshape(input_shape +
                                                             (-1,))

    elif method == 'ps':
      polysketch: PolyTensorSketch = input[0]
      tensorsrht: TensorSRHT = input[1]
      kappa0_coeff: np.ndarray = input[2][0]
      kappa1_coeff: np.ndarray = input[2][1]
      layer_count: int = input[2][2]

      # Apply PolySketch to approximate feature maps of kappa0 & kappa1 kernels.
      polysketch_feats = polysketch.sketch(nngp_feat_2d)
      kappa1_feat = polysketch.expand_feats(polysketch_feats, kappa1_coeff)
      kappa0_feat = polysketch.expand_feats(polysketch_feats, kappa0_coeff)
      del polysketch_feats

      # Apply SRHT to kappa1_feat so that dimension of nngp_feat is poly_sketch_dim//2.
      nngp_feat = polysketch.standardsrht(kappa1_feat).reshape(input_shape +
                                                               (-1,))
      # Apply TensorSRHT to ntk_feat_2d and kappa0_feat to approximate their tensor product.
      ntk_feat = tensorsrht.sketch(ntk_feat_2d,
                                   kappa0_feat).reshape(input_shape + (-1,))

      # At the top ReluFeatures, convert complex features to real ones.
      if top_layer:
        ntk_feat = np.concatenate((ntk_feat.real, ntk_feat.imag), axis=1)
        nngp_feat = np.concatenate((nngp_feat.real, nngp_feat.imag), axis=1)

    elif method == 'psrf':  # Combination of PolySketch and Random Features.
      W0: np.ndarray = input[0]
      polysketch: PolyTensorSketch = input[1]
      tensorsrht: TensorSRHT = input[2]
      kappa1_coeff: np.ndarray = input[3][0]

      polysketch_feats = polysketch.sketch(nngp_feat_2d)
      kappa1_feat = polysketch.expand_feats(polysketch_feats, kappa1_coeff)
      del polysketch_feats

      nngp_feat = polysketch.standardsrht(kappa1_feat).reshape(input_shape +
                                                               (-1,))

      nngp_proj = np.concatenate(
          (nngp_feat_2d.real, nngp_feat_2d.imag), axis=1) @ W0
      kappa0_feat = np.concatenate(
          ((nngp_proj > 0), (nngp_proj <= 0)), axis=1) / W0.shape[-1]**0.5
      del W0

      # Apply TensorSRHT to ntk_feat_2d and kappa0_feat to approximate their tensor product.
      ntk_feat = tensorsrht.sketch(ntk_feat_2d,
                                   kappa0_feat).reshape(input_shape + (-1,))

      # At the top ReluFeatures, convert complex features to real ones.
      if top_layer:
        ntk_feat = np.concatenate((ntk_feat.real, ntk_feat.imag), axis=1)
        nngp_feat = np.concatenate((nngp_feat.real, nngp_feat.imag), axis=1)

    elif method == 'poly':  # Polynomial approximation without sketching.
      kappa0_coeff: np.ndarray = input[0]
      kappa1_coeff: np.ndarray = input[1]
      layer_count = input[2]

      gram_nngp = np.dot(nngp_feat_2d, nngp_feat_2d.T)
      nngp_feat = cholesky(np.polyval(kappa1_coeff[::-1],
                                      gram_nngp)).reshape(input_shape + (-1,))

      ntk = ntk_feat_2d @ ntk_feat_2d.T
      kappa0_mat = np.polyval(kappa0_coeff[::-1], gram_nngp)
      ntk_feat = cholesky(ntk * kappa0_mat).reshape(input_shape + (-1,))

    elif method == 'exact':  # Exact feature map computations via Cholesky decomposition.
      nngp_feat = cholesky(kappa1(nngp_feat_2d)).reshape(input_shape + (-1,))

      ntk = ntk_feat_2d @ ntk_feat_2d.T
      kappa0_mat = kappa0(nngp_feat_2d)
      ntk_feat = cholesky(ntk * kappa0_mat).reshape(input_shape + (-1,))

    else:
      raise NotImplementedError(f'Invalid method name: {method}')

    if method != 'rf':
      norms /= 2.0**0.5

    return f.replace(nngp_feat=nngp_feat, ntk_feat=ntk_feat, norms=norms)

  return init_fn, feature_fn


def _prod(tuple_):
  prod = 1
  for x in tuple_:
    prod = prod * x
  return prod


@layer
def ReluNTKFeatures(
    num_layers: int,
    poly_degree: int = 16,
    poly_sketch_dim: int = 1024,
    W_std: float = 1.,
):

  def init_fn(rng, input_shape):
    input_dim = input_shape[0][-1]

    # PolySketch expansion for nngp/ntk features.
    polysketch = PolyTensorSketch(rng=rng,
                                  input_dim=input_dim,
                                  sketch_dim=poly_sketch_dim,
                                  degree=poly_degree).init_sketches()  # pytype:disable=wrong-keyword-args

    nngp_coeffs, ntk_coeffs = relu_ntk_coeffs(poly_degree, num_layers)

    return (), (polysketch, nngp_coeffs, ntk_coeffs)

  def feature_fn(f, input=None, **kwargs):
    input_shape = f.nngp_feat.shape[:-1]

    polysketch: PolyTensorSketch = input[0]
    nngp_coeffs: np.ndarray = input[1]
    ntk_coeffs: np.ndarray = input[2]

    polysketch_feats = polysketch.sketch(
        f.nngp_feat)  # f.ntk_feat should be equal to f.nngp_feat.
    nngp_feat = polysketch.expand_feats(polysketch_feats, nngp_coeffs)
    ntk_feat = polysketch.expand_feats(polysketch_feats, ntk_coeffs)

    # Apply SRHT to features so that dimensions are poly_sketch_dim//2.
    nngp_feat = polysketch.standardsrht(nngp_feat).reshape(input_shape + (-1,))
    ntk_feat = polysketch.standardsrht(ntk_feat).reshape(input_shape + (-1,))

    # Convert complex features to real ones.
    ntk_feat = np.concatenate((ntk_feat.real, ntk_feat.imag), axis=1)
    nngp_feat = np.concatenate((nngp_feat.real, nngp_feat.imag), axis=1)

    norms = f.norms / 2.**(num_layers / 2) * (W_std**(num_layers + 1))

    return _renormalize_feature(
        f.replace(nngp_feat=nngp_feat, ntk_feat=ntk_feat, norms=norms))

  return init_fn, feature_fn


@layer
def ConvFeatures(out_chan: int,
                 filter_shape: Sequence[int],
                 strides: Optional[Sequence[int]],
                 padding: str,
                 W_std: float = 1.0,
                 b_std: Optional[float] = None,
                 dimension_numbers: Optional[Tuple[str, str, str]] = None,
                 parameterization: str = 'ntk'):

  if b_std is not None:
    raise NotImplementedError('Bias variable b_std is not implemented yet .'
                              ' Please set b_std to be `0`.')

  parameterization = parameterization.lower()

  if dimension_numbers is None:
    dimension_numbers = _get_dimension_numbers(len(filter_shape), False)

  lhs_spec, rhs_spec, out_spec = dimension_numbers

  channel_axis = lhs_spec.index('C')

  if parameterization != 'ntk':
    raise NotImplementedError(f'Parameterization ({parameterization}) is '
                              ' not implemented yet.')

  if filter_shape[0] != filter_shape[1]:
    raise NotImplementedError('filter_shape should be square.')
  filter_size = filter_shape[0]

  def init_fn(rng, input_shape):
    nngp_feat_shape, ntk_feat_shape = input_shape[0], input_shape[1]
    new_nngp_feat_shape = nngp_feat_shape[:-1] + (nngp_feat_shape[-1] *
                                                  filter_size**2,)
    new_ntk_feat_shape = nngp_feat_shape[:-1] + (
        (nngp_feat_shape[-1] + ntk_feat_shape[-1]) * filter_size**2,)

    if len(input_shape) > 2:
      return (new_nngp_feat_shape, new_ntk_feat_shape, input_shape[2] + 'D'), ()
    else:
      return (new_nngp_feat_shape, new_ntk_feat_shape, 'D'), ()

  def feature_fn(f, input, **kwargs):
    nngp_feat, ntk_feat = f.nngp_feat, f.ntk_feat

    nngp_feat = _conv2d_feat(nngp_feat, filter_size) / filter_size * W_std

    if ntk_feat.ndim == 0:  # check if ntk_feat is empty
      ntk_feat = nngp_feat
    else:
      ntk_feat = _conv2d_feat(ntk_feat, filter_size) / filter_size * W_std
      ntk_feat = np.concatenate((ntk_feat, nngp_feat), axis=channel_axis)

    return f.replace(nngp_feat=nngp_feat, ntk_feat=ntk_feat)

  return init_fn, feature_fn


def _conv2d_feat(X, filter_size):
  return _conv_feat(np.moveaxis(_conv_feat(X, filter_size), 1, 2), filter_size)


def _conv_feat(X, filter_size):
  """
  Direct sums of image features. If input shape is (N, H, W, C), the output has
  the shape (N, H, W, C * filter_size**2).
  """
  N, H, W, C = X.shape
  out = np.zeros((N, H, W, C * filter_size))
  out = out.at[:, :, :, :C].set(X)
  j = 1
  for i in range(1, min((filter_size + 1) // 2, W)):
    out = out.at[:, :, :-i, j * C:(j + 1) * C].set(X[:, :, i:])
    j += 1
    out = out.at[:, :, i:, j * C:(j + 1) * C].set(X[:, :, :-i])
    j += 1
  return out


@layer
def AvgPoolFeatures(window_shape: Sequence[int],
                    strides: Optional[Sequence[int]] = None,
                    padding: str = stax.Padding.VALID.name,
                    normalize_edges: bool = False,
                    batch_axis: int = 0,
                    channel_axis: int = -1):

  def init_fn(rng, input_shape):
    nngp_feat_shape, ntk_feat_shape = input_shape[0], input_shape[1]

    new_nngp_feat_shape = nngp_feat_shape[:1] + (
        nngp_feat_shape[1] // window_shape[0],
        nngp_feat_shape[2] // window_shape[1]) + nngp_feat_shape[-1:]
    new_ntk_feat_shape = ntk_feat_shape[:1] + (
        ntk_feat_shape[1] // window_shape[0],
        ntk_feat_shape[2] // window_shape[1]) + ntk_feat_shape[-1:]
    return (new_nngp_feat_shape, new_ntk_feat_shape), ()

  def feature_fn(f, input=None, **kwargs):
    window_shape_kernel = (1,) + tuple(window_shape) + (1,)
    strides_kernel = (1,) + tuple(strides) + (1,)
    pooling = lambda x: _pool_kernel(x, Pooling.AVG,
                                     window_shape_kernel, strides_kernel,
                                     Padding(padding), normalize_edges, 0)
    nngp_feat = pooling(f.nngp_feat)

    if f.ntk_feat.ndim == 0:  # check if ntk_feat is empty
      ntk_feat = nngp_feat
    else:
      ntk_feat = pooling(f.ntk_feat)

    return f.replace(nngp_feat=nngp_feat, ntk_feat=ntk_feat)

  return init_fn, feature_fn


@layer
def FlattenFeatures(batch_axis: int = 0, batch_axis_out: int = 0):

  if batch_axis_out in (0, -2):
    batch_axis_out = 0
    channel_axis_out = 1
  elif batch_axis_out in (1, -1):
    batch_axis_out = 1
    channel_axis_out = 0
  else:
    raise ValueError(f'`batch_axis_out` must be 0 or 1, got {batch_axis_out}.')

  def init_fn(rng, input_shape):
    nngp_feat_shape, ntk_feat_shape = input_shape[0], input_shape[1]
    new_nngp_feat_shape = nngp_feat_shape[:1] + (_prod(nngp_feat_shape[1:]),)
    new_ntk_feat_shape = ntk_feat_shape[:1] + (_prod(ntk_feat_shape[1:]),)
    return (new_nngp_feat_shape, new_ntk_feat_shape), ()

  def feature_fn(f, input=None, **kwargs):
    batch_size = f.nngp_feat.shape[batch_axis]
    nngp_feat = f.nngp_feat.reshape(batch_size, -1) / _prod(
        f.nngp_feat.shape[1:-1])**0.5

    if f.ntk_feat.ndim == 0:  # check if ntk_feat is empty
      ntk_feat = f.ntk_feat
    else:
      ntk_feat = f.ntk_feat.reshape(batch_size, -1) / _prod(
          f.ntk_feat.shape[1:-1])**0.5

    return f.replace(nngp_feat=nngp_feat, ntk_feat=ntk_feat)

  return init_fn, feature_fn
