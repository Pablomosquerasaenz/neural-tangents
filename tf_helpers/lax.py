# Copyright 2020 The Google/TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


"""
This file contains some TF-based lax utilities. Some utilities, except
general convolution, general dot and reduce window and their dependencies,
are adpated from https://github.com/google/jax/blob/master/jax/lax/lax.py.
"""


import builtins
from typing import NamedTuple, Sequence
import string
import numpy as onp
from tensorflow.python.ops import numpy_ops as np
import tensorflow as tf
from tensorflow import nn
from tf_helpers.extensions import tf_dot_general
import sys


def conv_shape_tuple(lhs_shape, rhs_shape, strides, pads, batch_group_count=1):
  """Compute the shape tuple of a conv given input shapes in canonical order."""
  if isinstance(pads, str):
    pads = padtype_to_pads(lhs_shape[2:], rhs_shape[2:], strides, pads)
  if len(pads) != len(lhs_shape) - 2:
    msg = 'Wrong number of explicit pads for convolution: expected {}, got {}.'
    raise TypeError(msg.format(len(lhs_shape) - 2, len(pads)))

  lhs_padded = onp.add(lhs_shape[2:], np.sum(np.array(pads).reshape(-1, 2),
                                              axis=1))
  out_space = np.floor_divide(
    np.subtract(lhs_padded, rhs_shape[2:]), strides) + 1
  out_space = np.maximum(0, out_space)
  assert lhs_shape[0] % batch_group_count == 0
  out_shape = (lhs_shape[0] // batch_group_count, rhs_shape[0])
  return tuple(out_shape + tuple(out_space))


class ConvDimensionNumbers(NamedTuple):
  """Describes batch, spatial, and feature dimensions of a convolution.
  Args:
    lhs_spec: a tuple of nonnegative integer dimension numbers containing
      `(batch dimension, feature dimension, spatial dimensions...)`.
    rhs_spec: a tuple of nonnegative integer dimension numbers containing
      `(out feature dimension, in feature dimension, spatial dimensions...)`.
    out_spec: a tuple of nonnegative integer dimension numbers containing
      `(batch dimension, feature dimension, spatial dimensions...)`.
  """
  lhs_spec: Sequence[int]
  rhs_spec: Sequence[int]
  out_spec: Sequence[int]


def conv_general_permutations(dimension_numbers):
  """Utility for convolution dimension permutations relative to Conv HLO."""
  lhs_spec, rhs_spec, out_spec = dimension_numbers
  lhs_char, rhs_char, out_char = charpairs = ('N', 'C'), ('O', 'I'), ('N', 'C')
  for i, (a, b) in enumerate(charpairs):
    if not dimension_numbers[i].count(a) == dimension_numbers[i].count(b) == 1:
      msg = ('convolution dimension_numbers[{}] must contain the characters '
             '`{}` and `{}` exactly once, got {}.')
      raise TypeError(msg.format(i, a, b, dimension_numbers[i]))
    if len(dimension_numbers[i]) != len(set(dimension_numbers[i])):
      msg = ('convolution dimension_numbers[{}] cannot have duplicate '
             'characters, got {}.')
      raise TypeError(msg.format(i, dimension_numbers[i]))
  if not (set(lhs_spec) - set(lhs_char) == set(rhs_spec) - set(rhs_char) ==
          set(out_spec) - set(out_char)):
    msg = ('convolution dimension_numbers elements must each have the same '
           'set of spatial characters, got {}.')
    raise TypeError(msg.format(dimension_numbers))

  def getperm(spec, charpair):
    spatial = (i for i, c in enumerate(spec) if c not in charpair)
    if spec is not rhs_spec:
      spatial = sorted(spatial, key=lambda i: rhs_spec.index(spec[i]))
    return (spec.index(charpair[0]), spec.index(charpair[1])) + tuple(spatial)

  lhs_perm, rhs_perm, out_perm = map(getperm, dimension_numbers, charpairs)
  return lhs_perm, rhs_perm, out_perm


def conv_dimension_numbers(lhs_shape, rhs_shape, dimension_numbers):
  """Converts convolution `dimension_numbers` to a `ConvDimensionNumbers`.
  Args:
    lhs_shape: tuple of nonnegative integers, shape of the convolution input.
    rhs_shape: tuple of nonnegative integers, shape of the convolution kernel.
    dimension_numbers: None or a tuple/list of strings or a ConvDimensionNumbers
      object following the convolution dimension number specification format in
      xla_client.py.
  Returns:
    A `ConvDimensionNumbers` object that represents `dimension_numbers` in the
    canonical form used by lax functions.
  """
  if isinstance(dimension_numbers, ConvDimensionNumbers):
    return dimension_numbers
  if len(lhs_shape) != len(rhs_shape):
    msg = 'convolution requires lhs and rhs ndim to be equal, got {} and {}.'
    raise TypeError(msg.format(len(lhs_shape), len(rhs_shape)))

  if dimension_numbers is None:
    iota = tuple(range(len(lhs_shape)))
    return ConvDimensionNumbers(iota, iota, iota)
  elif isinstance(dimension_numbers, (list, tuple)):
    if len(dimension_numbers) != 3:
      msg = 'convolution dimension_numbers list/tuple must be length 3, got {}.'
      raise TypeError(msg.format(len(dimension_numbers)))
    if not all(isinstance(elt, str) for elt in dimension_numbers):
      msg = 'convolution dimension_numbers elements must be strings, got {}.'
      raise TypeError(msg.format(tuple(map(type, dimension_numbers))))
    msg = ('convolution dimension_numbers[{}] must have len equal to the ndim '
           'of lhs and rhs, got {} for lhs and rhs shapes {} and {}.')
    for i, elt in enumerate(dimension_numbers):
      if len(elt) != len(lhs_shape):
        raise TypeError(msg.format(i, len(elt), lhs_shape, rhs_shape))

    lhs_spec, rhs_spec, out_spec = conv_general_permutations(dimension_numbers)
    return ConvDimensionNumbers(lhs_spec, rhs_spec, out_spec)
  else:
    msg = 'convolution dimension_numbers must be tuple/list or None, got {}.'
    raise TypeError(msg.format(type(dimension_numbers)))


def padtype_to_pads(in_shape, window_shape, window_strides, padding):
  if padding == 'SAME':
    out_shape = _ceil_divide(in_shape, window_strides)
    pad_sizes = np.maximum(0, (out_shape - 1) * window_strides +
                              window_shape - in_shape)
    return [(pad_size // 2, pad_size - pad_size // 2) for pad_size in pad_sizes]
  elif padding == 'VALID':
    return [(0, 0)] * len(in_shape)


def conv_general_shape_tuple(lhs_shape, rhs_shape, window_strides, padding,
                             dimension_numbers):
  lhs_perm, rhs_perm, out_perm = conv_general_permutations(dimension_numbers)
  lhs_trans = np.take(lhs_shape, lhs_perm)
  rhs_trans = np.take(rhs_shape, rhs_perm)
  out_trans = conv_shape_tuple(lhs_trans, rhs_trans, window_strides, padding)
  return tuple(np.take(out_trans, np.argsort(out_perm)))


def conv_transpose_shape_tuple(lhs_shape, rhs_shape, window_strides, padding,
                               dimension_numbers):
  lhs_perm, rhs_perm, out_perm = conv_general_permutations(dimension_numbers)
  lhs_trans = np.take(lhs_shape, lhs_perm)
  rhs_trans = np.take(rhs_shape, rhs_perm)
  if isinstance(padding, str):
    padding = [_conv_transpose_padding(k, s, padding)
               for k,s in zip(rhs_trans[2:], window_strides)]
  padding = list(map(np.sum, padding))
  unpad_out_space = [(i-1) * s - k + 2
                     for i, k, s in zip(lhs_trans[2:],
                                        rhs_trans[2:],
                                        window_strides)]
  out_space = np.sum([unpad_out_space, padding], axis=0).tolist()
  out_trans = tuple((lhs_trans[0], rhs_trans[0]) + tuple(out_space))
  return tuple(np.take(out_trans, np.argsort(out_perm)))


def reduce_window_shape_tuple(operand_shape, window_dimensions, window_strides,
                              padding):
  window_dimensions = (1,) + window_dimensions + (1,)
  window_strides = (1,) + window_strides + (1,)
  pads = padtype_to_pads(operand_shape, window_dimensions, window_strides, padding)
  operand_padded = np.add(operand_shape, np.add(*zip(*pads)))
  t = np.floor_divide(
      np.subtract(operand_padded, window_dimensions), window_strides) + 1
  return tuple(t)


def conv_transpose(lhs, rhs, strides, padding,
                   rhs_dilation=None, dimension_numbers=None,
                   transpose_kernel=False, precision=None):
  """Convenience wrapper for calculating the N-d convolution "transpose".
  This function directly calculates a fractionally strided conv rather than
  indirectly calculating the gradient (transpose) of a forward convolution.

  Args:
    lhs: a rank `n+2` dimensional input array.
    rhs: a rank `n+2` dimensional array of kernel weights.
    strides: sequence of `n` integers, sets fractional stride.
    padding: 'SAME', 'VALID' will set as transpose of corresponding forward
      conv, or a sequence of `n` integer 2-tuples describing before-and-after
      padding for each `n` spatial dimension.
    rhs_dilation: `None`, or a sequence of `n` integers, giving the
      dilation factor to apply in each spatial dimension of `rhs`. RHS dilation
      is also known as atrous convolution.
    dimension_numbers: tuple of dimension descriptors as in
      lax.conv_general_dilated. Defaults to tensorflow convention.
    transpose_kernel: if True flips spatial axes and swaps the input/output
      channel axes of the kernel. This makes the output of this function identical
      to the gradient-derived functions like keras.layers.Conv2DTranspose
      applied to the same kernel. For typical use in neural nets this is completely
      pointless and just makes input/output channel specification confusing.
    precision: Optional. Either `None`, which means the default precision for
      the backend, or a `Precision` enum value.

  Returns:
    Transposed N-d convolution, with output padding following the conventions of
    keras.layers.Conv2DTranspose.
  """
  assert len(lhs.shape) == len(rhs.shape) and len(lhs.shape) > 2
  ndims = len(lhs.shape)
  one = (1,) * (ndims - 2)
  # Set dimensional layout defaults if not specified.
  if dimension_numbers is None:
    if ndims == 3:
      dimension_numbers = ('NHC', 'HIO', 'NHC')
    elif ndims == 4:
      dimension_numbers = ('NHWC', 'HWIO', 'NHWC')
    elif ndims == 5:
      dimension_numbers = ('NHWDC', 'HWDIO', 'NHWDC')
    else:
      raise ValueError('No 4+ dimensional dimension_number defaults.')
  dn = conv_dimension_numbers(lhs.shape, rhs.shape, dimension_numbers)
  k_shape = np.take(rhs.shape, dn.rhs_spec)
  k_sdims = k_shape[2:]
  # Calculate correct output shape given padding and strides.
  pads: Union[str, Sequence[Tuple[int, int]]]
  if padding in {'SAME', 'VALID'}:
    if rhs_dilation is None:
      rhs_dilation = (1,) * (rhs.ndim - 2)
    effective_k_size = map(lambda k, r: (k-1) * r + 1, k_sdims, rhs_dilation)
    pads = [_conv_transpose_padding(k, s, padding)
            for k,s in zip(effective_k_size, strides)]
  else:
    pads = padding
  if transpose_kernel:
    # flip spatial dims and swap input / output channel axes
    rhs = _flip_axes(rhs, np.array(dn.rhs_spec)[2:])
    rhs = np.swapaxes(rhs, dn.rhs_spec[0], dn.rhs_spec[1])
  return conv_general_dilated(lhs, rhs, one, pads, strides, rhs_dilation, dn)


def dot_general(lhs, rhs, dimension_numbers, precision=None):
  """ The general dot operation for TensorFlow.

  An equivalent general dot operation as that in JAX -
     <https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.dot_general.html>
  Although there is an implementation in TF XLA, avoid directly using XLA when
  possible.

  e.g., non-batched: ij,jk->ik
        batched: ijk,ikl->ijl

  Args:
    lhs: an array (the left-hand side matrix/vector to be multiplied)
    rhs: an array (the right-hand side matrix/vector to be multiplied)
    dimension_numbers: (Tuple[Tuple[Sequence[int], Sequence[int]],
      Tuple[Sequence[int], Sequence[int]]]) – a tuple of tuples of the form
      ((lhs_contracting_dims, rhs_contracting_dims), (lhs_batch_dims, rhs_batch_dims))

  Returns:
    An array that contains the result.
  """
  return tf_dot_general(lhs, rhs, dimension_numbers, precision)


def reduce_window(inputs, init_value, reducer, window_dimensions, strides,
                  padding, base_dilation=None, window_dilation=None):
  # Add an extra "batch" dimension and an extra "channel" dimension to pass the
  # TensorFlow pool dimensionality checker.
  inputs = np.expand_dims(inputs, axis=(0, inputs.ndim))
  if reducer not in [np.max, np.add]:
    raise TypeError('Only max pooling and average/sum pooling are supported.')

  # Note that there is no need to send in the parameter data format since the
  # input is already of default data format - 'N...C'. The adjustments of the
  # input shape is already finished in apply_fun of Pooling in stax.
  pooling = 'AVG' if reducer == np.add else 'MAX'
  output = np.asarray(nn.pool(inputs, window_dimensions, pooling, strides, padding))
  return np.squeeze(output, axis=(0, output.ndim - 1)) * np.prod(window_dimensions)


# TOTO (Zhibo Zhang): Expand the test cases of general convolution and revise
#                     the according bugs.
# TODO (Zhibo Zhang): Support feature_group_count, batch_group_count and precision, and
#       allow lhs_dilation and rhs_dilation to happen at the same time.
def conv_general_dilated(lhs, rhs, window_strides, padding, output_shape, lhs_dilation=None,
                         rhs_dilation=None, dimension_numbers=None,
                         feature_group_count=1, batch_group_count=1, precision=None):
  """ A general conv API that integrates normal conv, deconvolution,
  dilated convolution, etc."""
  dim = None
  lhs_spec, rhs_spec, out_spec = dimension_numbers
  if lhs_spec != out_spec:
    raise TypeError('Current implementation requires the `data_format` of the '
                    'inputs and outputs to be the same.')
  if len(lhs_spec) >= 6:
    raise TypeError('Current implmentation does not support 4 or higher'
                    'dimensional convolution, but got: ', len(lhs_spec) - 2)
  dim = len(lhs_spec) - 2
  if lhs_dilation and rhs_dilation:
    if lhs_dilation == (1,) * dim and rhs_dilation == (1,) * dim:
      lhs_dilation, rhs_dilation = None, None
    else:
      raise TypeError('Current implementation does not support that '
                      'deconvolution and dilation to be performed at the same '
                      'time, but got lhs_dilation: {}, rhs_dilation: {}'.format(
                          lhs_dilation, rhs_dilation))
  if padding not in ['SAME', 'VALID']:
    raise TypeError('Current implementation requires the padding parameter'
                    'to be either `VALID` or `SAME`, but got: ', padding)
  # Convert params from int/Sequence[int] to list of ints.
  strides, lhs_dilation, rhs_dilation = _conv_general_param_type_converter(
    window_strides, lhs_dilation, rhs_dilation
  )
  # Preprocess the shapes
  dim_maps = {}
  if isinstance(lhs_spec, str):
    dim_maps['I'] = list(rhs_spec).index('I')
    dim_maps['O'] = list(rhs_spec).index('O')
    dim_maps['N'] = list(lhs_spec).index('N')
    dim_maps['C'] = list(lhs_spec).index('C')
  else:
    dim_maps['I'] = rhs_spec[1]
    dim_maps['O'] = rhs_spec[0]
    dim_maps['N'] = lhs_spec[0]
    dim_maps['C'] = lhs_spec[1]

  lhs = np.moveaxis(lhs, (dim_maps['N'], dim_maps['C']), (0, dim + 1))
  # Adjust the filters, put the dimension 'I' and 'O' at last.
  rhs = np.moveaxis(rhs, (dim_maps['O'], dim_maps['I']), (dim + 1, dim))
  spatial_dim_maps = {1: 'W', 2: 'HW', 3: 'DHW'}
  data_format = 'N' + spatial_dim_maps[dim] + 'C'
  tf_nn_APIs = {1: [nn.conv1d, nn.conv1d_transpose],
                2: [nn.conv2d, nn.conv2d_transpose],
                3: [nn.conv3d, nn.conv3d_transpose]}

  output = None
  if rhs_dilation or (lhs_dilation is None and rhs_dilation is None):
    output = tf_nn_APIs[dim][0](lhs, rhs, strides, padding, data_format, rhs_dilation)
  else:
    output = tf_nn_APIs[dim][1](lhs, rhs, tf.constant(output_shape), strides, padding, data_format, lhs_dilation)
  output = np.moveaxis(output, (0, dim + 1), (dim_maps['N'], dim_maps['C']))
  return np.asarray(output)


def _ceil_divide(x1, x2):
  return -np.floor_divide(np.negative(x1), x2)


def _conv_transpose_padding(k, s, padding):
  if padding == 'SAME':
    pad_len = k + s - 2
    if s > k - 1:
      pad_a = k - 1
    else:
      pad_a = int(np.ceil(pad_len / 2))
  elif padding == 'VALID':
    pad_len = k + s - 2 + max(k - s, 0)
    pad_a = k - 1
  else:
    raise ValueError('Padding mode must be `SAME` or `VALID`.')
  pad_b = pad_len - pad_a
  return pad_a, pad_b


def _conv_general_param_type_converter(window_strides, lhs_dilation, rhs_dilation):
  """ Convert the inputs strides, lhs_dilation, rhs_dilation to the standard
  TF conv inputs.

  For example,
   in the 3D case, if lhs_dilation = 2, then convert it to [2, 2, 2]
                   if lhs_dilation = (2, 2, 2), convert it also to [2, 2, 2]
  """
  strides = [window_strides] * dim if isinstance(window_strides, int) else \
            list(window_strides)
  if lhs_dilation:
    lhs_dilation = [lhs_dilation] * dim if isinstance(lhs_dilation, int) else \
                    list(lhs_dilation)
  if rhs_dilation:
    rhs_dilation = [rhs_dilation] * dim if isinstance(rhs_dilation, int) else \
                    list(rhs_dilation)
  return (strides, lhs_dilation, rhs_dilation)


def _eval_output_shape(lhs_shape, rhs_shape, padding, window_strides):
  """ Evaluate the output shape in for transpose convolutions.
  """
  output_shape = [lhs_shape[0]]
  for i in range(1, len(lhs_shape) - 1):
    if padding == 'SAME':
      output_shape.append((lhs_shape[i] - 1) * window_strides[i-1] + rhs_shape[i])
    if padding == 'VALID':
      output_shape.append((lhs_shape[i] - 1) * window_strides[i-1])
  output_shape.append(lhs_shape[-1])
  return tf.constant(output_shape)
