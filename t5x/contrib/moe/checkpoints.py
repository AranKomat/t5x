# Copyright 2022 The T5X Authors.
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

"""Mixture-of-Experts checkpoint overrides."""

import functools
import os
from typing import Any, Optional, Union

import clu.data
from jax.experimental import global_device_array as gda_lib
from jax.experimental.gda_serialization import serialization as gda_serialization
import jax.numpy as jnp
import numpy as np
from t5x import checkpoint_importer
from t5x import checkpoints
from t5x import partitioning
from t5x import train_state as train_state_lib
import tensorflow as tf
import tensorstore as ts

LazyAwaitableArray = checkpoint_importer.LazyAwaitableArray
_ParameterInfo = checkpoints._ParameterInfo  # pylint: disable=protected-access


class D2SCheckpointer(checkpoints.Checkpointer):
  """Modified Checkpointer for dense-to-sparse runs.

  This subclass calls modified _read_ts, namely _read_d2s_ts, which broadcasts
  the checkpoint's dense MLP weights to the model's sparse, expert weights. This
  enables sparsifying dense checkpoints. See also _read_d2s_ts for more details.
  """

  def __init__(
      self,
      train_state: train_state_lib.TrainState,
      partitioner: partitioning.BasePartitioner,
      checkpoints_dir: str,
      dataset_iterator: Optional[Union[tf.data.Iterator,
                                       clu.data.DatasetIterator]] = None,
      *,
      keep: Optional[int] = None,
      save_dtype: jnp.dtype = np.float32,
      restore_dtype: Optional[jnp.dtype] = None,
      use_gda: Optional[bool] = False,
      keep_dataset_checkpoints: Optional[int] = None):
    """Checkpointer constructor.

    Args:
      train_state: A train state to be used to determine the structure of the
        parameter tree, and the *full* (non-partitioned) parameter shapes and
        dtypes. Saved and restored train states must match this structure.
      partitioner: The partitioner to use for determining the local chunks
        mapping or to perform params partitioning on restore.
      checkpoints_dir: a path to a directory to save checkpoints in and restore
        them from.
      dataset_iterator: An optional iterator to save/restore.
      keep: An optional maximum number of checkpoints to keep. If more than this
        number of checkpoints exist after a save, the oldest ones will be
        automatically deleted to save space.
      save_dtype: Dtype to cast targets to before saving.
      restore_dtype: Optional dtype to cast targets to after restoring. If None,
        no parameter casting is performed.
      use_gda: If True, enabled gda_lib.GlobalDeviceArray. Note: this is
        currently an experimental feature under development.
      keep_dataset_checkpoints: An optional maximum number of data iterators to
        keep. If more than this number of data iterators exist after a save, the
        oldest ones will be automatically deleted to save space.
    """

    super().__init__(
        train_state=train_state,
        partitioner=partitioner,
        checkpoints_dir=checkpoints_dir,
        dataset_iterator=dataset_iterator,
        keep=keep,
        save_dtype=save_dtype,
        restore_dtype=restore_dtype,
        use_gda=use_gda,
        keep_dataset_checkpoints=keep_dataset_checkpoints)

  def _create_lazy_awaitable_array(
      self, param_info: _ParameterInfo, maybe_ts_spec: Any, ckpt_path: str,
      restore_dtype: Optional[jnp.dtype]) -> LazyAwaitableArray:
    """Creates LazyArray from tensorstore and optionally broadcasts it.

    Does not materialize the array immediately.

    The only difference of this method from that of the parent class is that
    this one calls _read_d2s_ts instead of _read_ts, which also performs
    broadcasting the MoE weights and optimizer states for dense-to-sparse
    models.

    Args:
      param_info: Information about how to read the parameter, host based sliced
        reads and the like.
      maybe_ts_spec: The tensorstore spec to read the parameter or some other
        object. If this is an array then we will do a host based sliced read on
        it (provided the param_info says to). Anything else we just return.
      ckpt_path: A base location to use when resolving the relative paths in the
        tensorstore spec.
      restore_dtype: Type to restore as. None indicates that no cast is
        requested.

    Returns:
      LazyArray object. If it is an expert parameter, then it is broadcast to
       all experts.
    """
    mesh = None
    axes = None
    if self._use_gda:
      mesh = self._partitioner.mesh
      axes = param_info.axes
    get_fn = functools.partial(
        _read_d2s_ts,
        param_info,
        maybe_ts_spec,
        ckpt_path=ckpt_path,
        restore_dtype=restore_dtype,
        mesh=mesh,
        axes=axes)
    return LazyAwaitableArray.from_tensor_store_spec_or_array(
        maybe_ts_spec, get_fn, dtype=restore_dtype)


def _broadcast_ffn(arr, num_experts_per_slice):
  """Broadcasts arr by the factor of num_experts_per_slice."""
  return np.repeat(arr[None], num_experts_per_slice, axis=0)


async def _read_d2s_slice_from_ts(compressed_arr, param_info):
  """Reads dense-to-sparse array from tensorstore and broadcasts to experts."""
  sl = param_info.local_chunk_info.slice
  # Since sl is the slice generated for the new model, we need to ignore
  # the first (expert) dimension to deal with the checkpoint states.
  compressed_arr = compressed_arr[sl[1:]]
  # TODO(akom): Find a more principled way of computing this mesh and
  #  num_experts dependent quantity.
  # Compute the number of entries in sl[0] by applying it to a sufficiently
  # long list (i.e. range(1024))
  num_experts_per_slice = len(list(range(1024))[sl[0]])
  uncompressed_arr = await compressed_arr.read()
  return _broadcast_ffn(uncompressed_arr, num_experts_per_slice)


async def _read_d2s_ts(param_info: _ParameterInfo,
                       maybe_ts_spec: Any,
                       ckpt_path: str,
                       restore_dtype: Optional[jnp.dtype] = None,
                       mesh: Optional[gda_lib.Shape] = None,
                       axes: Optional[gda_lib.MeshAxes] = None):
  """Reads array from tensorstore and handles broadcasting of expert weights.

  This method is adapted from _read_ts() in t5x/checkpoints.py. This variant
  broadcasts dense MLP weights from the checkpoint to the sparse, expert weights
  of the model.

  Args:
    param_info: Information about how to read the parameter, host based sliced
      reads and the like.
    maybe_ts_spec: The tensorstore spec to read the parameter or some other
      object. If this is an array then we will do a host based sliced read on it
      (provided the param_info says to). Anything else we just return.
    ckpt_path: A base location to use when resolving the relative paths in the
      tensorstore spec.
    restore_dtype: type to restore as. None indicates that no cast is requested.
    mesh: Mesh object for GDA restoration.
    axes: MeshAxes object for GDA restoration.

  Returns:
    The array. Depending on the value `maybe_ts_spec` it might be read from
    tensorstore, or it might be returned as is. Depending on the values in
    param_info (specifically the `local_chunk_info`) it might be the full value
    or a specific slice. If it is an expert parameter, then it is broadcast to
    all experts.
  """
  if param_info:
    param_name = param_info.name
    m_or_v = param_name.endswith('/m') or param_name.endswith('/v')
    is_expert_param = 'expert/' in param_name

  def read_d2s_slice_from_np(compressed_arr):
    """Reads slice from numpy array and broadcasts to experts."""
    # Just read the subsection we care about.
    sl = param_info.local_chunk_info.slice
    # Since sl is the slice generated for the new model, we need to ignore
    # the first (expert) dimension to deal with the checkpoint states.
    arr_slice = compressed_arr[sl[1:]]
    # TODO(akom): Find a more principled way of computing this mesh and
    #  num_experts dependent quantity.
    # Compute the number of entries in sl[0] by applying it to a sufficiently
    # long list (i.e. range(1024))
    num_experts_per_slice = len(list(range(1024))[sl[0]])
    return _broadcast_ffn(arr_slice, num_experts_per_slice)

  # If saved as a numpy array, but a partitioned read is requested, return a
  # slice of the array for that host. Otherwise, return the whole thing.
  if isinstance(maybe_ts_spec, np.ndarray) and param_info:
    if param_info.local_chunk_info:
      arr = maybe_ts_spec
      if (not m_or_v) and is_expert_param:
        return read_d2s_slice_from_np(arr)
      else:
        arr = maybe_ts_spec
        return arr[param_info.local_chunk_info.slice]
    else:
      return maybe_ts_spec
  # If we have anything else that isn't a tensorstore spec just return it.
  elif not isinstance(maybe_ts_spec, ts.Spec):
    return maybe_ts_spec

  tmp_ts_spec_dict = maybe_ts_spec.to_json()
  # Remove non-required params so that we can open Tensorstore
  # that was created with a different set of params.
  del tmp_ts_spec_dict['metadata']['chunks']
  del tmp_ts_spec_dict['metadata']['compressor']

  # Convert the relative path in the spec to a path based on the checkpoint
  # location. Path and gcs bucket (if applicable) information is updated
  # in-place.
  checkpoints._update_ts_path_from_relative_to_absolute(  # pylint:disable=protected-access
      os.path.dirname(ckpt_path), tmp_ts_spec_dict)

  if param_info.shape is not None:
    ts_spec_arr_shape = tuple(tmp_ts_spec_dict['metadata']['shape'])
    # Check that the shapes of the array on disk match the expected shape based
    # on the optimizer that is being restored.
    if (not m_or_v) and is_expert_param:
      shapes_match = ts_spec_arr_shape == param_info.shape[1:]
    else:
      shapes_match = ts_spec_arr_shape == param_info.shape
    if not shapes_match:
      raise ValueError(f'Shape of `{param_info.name}` in checkpoint '
                       f'{ts_spec_arr_shape} does not match expected '
                       f'{param_info.shape}.')

  if ('dtype' in tmp_ts_spec_dict and tmp_ts_spec_dict['dtype']
      == 'uint16') or ('dtype' in tmp_ts_spec_dict['metadata'] and
                       tmp_ts_spec_dict['metadata']['dtype'] == '<u2'):
    raise ValueError(
        f'Found unsupported uint16 type in Tensorstore spec: {tmp_ts_spec_dict}. '
        'Please use t5x/google/scripts/convert_uint16_checkpoint.py '
        'to update saved types to bfloat16.')

  if restore_dtype is not None:
    tmp_ts_spec_dict = {
        'base': tmp_ts_spec_dict,
        'driver': 'cast',
        'dtype': jnp.dtype(restore_dtype).name
    }

  if mesh is None or axes is None:
    # Read the array.
    t = await ts.open(tmp_ts_spec_dict, open=True)
    info = param_info.local_chunk_info is not None
    if (not m_or_v) and is_expert_param and info:
      arr = await _read_d2s_slice_from_ts(t, param_info)
    else:
      if param_info.local_chunk_info is not None:
        # Just read the subsection we care about.
        t = t[param_info.local_chunk_info.slice]
      arr = await t.read()
  else:
    # If provided, read as GDA.
    arr = await gda_serialization.async_deserialize(mesh, axes,
                                                    tmp_ts_spec_dict)
  return arr
