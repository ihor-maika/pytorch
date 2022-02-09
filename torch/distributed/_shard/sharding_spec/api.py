from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Union
from typing import TYPE_CHECKING

import torch

from ._internals import (
    check_tensor,
    get_chunked_dim_size,
    get_split_size,
    validate_non_overlapping_shards_metadata
)
from ..metadata import ShardMetadata

from torch.distributed._shard.sharded_tensor.utils import (
    _parse_and_validate_remote_device
)

import torch.distributed as dist
from torch.distributed import distributed_c10d
import torch.distributed._shard.sharded_tensor.metadata as sharded_tensor_meta
from torch.distributed._shard.sharded_tensor.shard import Shard

if TYPE_CHECKING:
    # Only include ShardedTensor when do type checking, exclude it
    # from run-time to resolve circular dependency.
    from torch.distributed._shard.sharded_tensor import ShardedTensor

class PlacementSpec(ABC):
    """
    Base class representing the placement of an entity. Subclasses of this
    class can be used to specify customized placements which might not be
    covered by existing APIs.
    """
    pass


@dataclass
class DevicePlacementSpec(PlacementSpec):
    """
    Associates placement of an entity with a single device.

    Args:
        device(:class:`torch.distributed._remote_device`): The device to place the entity on.
    """

    device: torch.distributed._remote_device

    def __post_init__(self):
        if not isinstance(self.device, torch.distributed._remote_device):
            self.device = torch.distributed._remote_device(self.device)

class ShardingSpec(object):
    """
    Base class representing sharding specifications.
    """
    @abstractmethod
    def build_metadata(self,
                       tensor_sizes: torch.Size,
                       tensor_properties: sharded_tensor_meta.TensorProperties,
                       process_group=None) -> sharded_tensor_meta.ShardedTensorMetadata:
        """
        Given a global tensor size list, define how to shard a tensor like this shape
        across ranks, return ShardedTensorMetadata
        """

    @abstractmethod
    def shard(self, tensor: torch.Tensor, src_rank: int = 0, process_group=None) -> "ShardedTensor":
        """
        Given a global tensor on src_rank, shard this tensor
        across ranks, return a ShardedTensor.
        """

@dataclass
class ChunkShardingSpec(ShardingSpec):
    """
    This is a type of PlacementSpec that defines the placement as being sharded
    across multiple devices. In particular, it represents sharding a Tensor
    along a single dimension into equal chunks (similar to :meth:`torch.chunk`).

    The semantics of how a tensor is partitioned is inline with
    :meth:`torch.chunk`, where ``dim`` in torch.chunk corresponds to the
    specified ``dim`` and ``chunks`` in torch.chunk is the number of elements
    in the placement specified.

    Args:
        dim (int or str):
            The dimension to shard on, could be an integer representing the
            dimension or a string in case of named tensors where dimensions are
            named.
        placement(List[Union[_remote_device, str]]):
            Specifies the placement of each shard of the Tensor. The size of
            the list represents the number of shards to be created. This could
            be a list of
            :class:`torch.distributed._remote_device`'s. This list
            could also contain a string which represents remote
            device as accepted by
            :class:`torch.distributed._remote_device`
    """

    ShardingDim = Union[int, str]

    dim: ShardingDim
    placements: List[Union[torch.distributed._remote_device, str]]

    def __post_init__(self):
        self._verify_dim(self.dim)
        for i, remote_device in enumerate(self.placements):
            if not isinstance(remote_device, torch.distributed._remote_device):
                self.placements[i] = torch.distributed._remote_device(remote_device)

    @staticmethod
    def _verify_dim(dim):
        # Validate the sharding spec.
        # TODO: support named dimension
        if isinstance(dim, str):
            raise NotImplementedError(
                "ChunkShardingSpec does not support named dimension yet!"
            )

        if not isinstance(dim, int):
            raise ValueError(
                f"Sharding dim needs to be an integer, found: {dim}"
            )

    def build_metadata(self,
                       tensor_sizes: torch.Size,
                       tensor_properties: sharded_tensor_meta.TensorProperties,
                       process_group=None) -> sharded_tensor_meta.ShardedTensorMetadata:
        """
        Given a global tensor size list, define how to shard a tensor like this shape
        across ranks, return ShardedTensorMetadata.
        """
        pg = process_group if process_group is not None else distributed_c10d._get_default_group()
        tensor_num_dim = len(tensor_sizes)

        self._verify_dim(self.dim)
        if self.dim >= tensor_num_dim or self.dim < -tensor_num_dim:
            raise ValueError(f"Invalid sharding dim: {self.dim}")

        shards_metadata = []
        sharding_dim_size = tensor_sizes[self.dim]
        chunks = len(self.placements)
        split_size = get_split_size(sharding_dim_size, chunks)
        for idx, placement in enumerate(self.placements):
            # check if the placement is valid or not
            # _parse_and_validate_remote_device(process_group, placement)
            # generate ShardMetadata for each placement device
            chunked_dim_size = get_chunked_dim_size(sharding_dim_size, split_size, idx)
            if chunked_dim_size > 0:
                shard_size = list(tensor_sizes)
                current_offsets = [0] * tensor_num_dim
                current_offsets[self.dim] = split_size * idx
                shard_size[self.dim] = chunked_dim_size  # type: ignore[index]

                shard_metadata = ShardMetadata(
                    shard_offsets=current_offsets,
                    shard_sizes=shard_size,
                    placement=placement,
                )
                shards_metadata.append(shard_metadata)

                # current_offsets[self.dim] += chunked_dim_size  # type: ignore[index]

        return sharded_tensor_meta.ShardedTensorMetadata(
            shards_metadata,
            tensor_sizes,
            tensor_properties
        )


    def shard(self, tensor: torch.Tensor, src_rank: int = 0, process_group=None) -> "ShardedTensor":
        """
        Given a global tensor on src_rank, shard this tensor
        across ranks, return a ShardedTensor.
        """
        from torch.distributed._shard.sharded_tensor import (
            ShardedTensor
        )
        tensor_properties = sharded_tensor_meta.TensorProperties(
            dtype=tensor.dtype,
            layout=tensor.layout,
            requires_grad=tensor.requires_grad,
            memory_format=torch.contiguous_format,
            pin_memory=tensor.is_pinned()
        )
        tensor_meta = self.build_metadata(tensor.size(), tensor_properties, process_group=process_group)
        local_shards = []

        current_rank = dist.get_rank(process_group)
        # Scatter the shards (use broadcast since NCCL doesn't support scatter, this is very inefficient).
        dist.broadcast(tensor, src=src_rank, group=process_group)

        for shard_meta in tensor_meta.shards_metadata:
            rank, device = _parse_and_validate_remote_device(process_group, shard_meta.placement)
            if rank == current_rank:
                shard_offsets = shard_meta.shard_offsets
                shard_sizes = shard_meta.shard_sizes
                local_tensor = tensor
                for idx, (offset, size) in enumerate(zip(shard_offsets, shard_sizes)):
                    if size < tensor.size(idx):
                        # Reshape to get shard for this rank and we don't want autograd
                        # recording here for the narrow op and 'local_shard' should be a
                        # leaf variable in the autograd graph.
                        local_tensor = local_tensor.narrow(
                            idx,
                            shard_offsets[idx],
                            shard_sizes[idx]
                        ).clone().detach().contiguous()
                # Sync requires_grad to local_shard.
                local_tensor.requires_grad = tensor.requires_grad
                local_shards.append(
                    Shard(
                        tensor=local_tensor,
                        metadata=shard_meta
                    )
                )

        st = ShardedTensor._init_from_local_shards(local_shards, tensor.size(), process_group=process_group)
        # Manually set sharding_spec
        st._sharding_spec = self

        return st


@dataclass
class EnumerableShardingSpec(ShardingSpec):
    """
    This is a type of PlacementSpec that allows users to specify a generic
    sharding scheme by enumerating exactly how each shard is laid out.

    Args:
        shards(List[ShardMetadata]): List of :class:`ShardMetadata` objects representing
            each shard. Note that none of the shards should overlap.
    """

    shards: List[ShardMetadata]

    def __post_init__(self):
        if len(self.shards) == 0:
            raise ValueError(f'Empty shard list provided: {self.shards}')

        # Validate each shard has same rank.
        rank = -1
        for shard in self.shards:
            if rank != -1 and rank != len(shard.shard_offsets):
                raise ValueError(f'Found inconsistent ranks for shards: {rank} and {len(shard.shard_offsets)}')
            rank = len(shard.shard_offsets)

        validate_non_overlapping_shards_metadata(self.shards)

    def build_metadata(self,
                       tensor_sizes: torch.Size,
                       tensor_properties: sharded_tensor_meta.TensorProperties,
                       process_group=None) -> sharded_tensor_meta.ShardedTensorMetadata:
        # check if shards form a valid tensor
        check_tensor(self.shards, tensor_sizes)
        return sharded_tensor_meta.ShardedTensorMetadata(
            self.shards,
            tensor_sizes,
            tensor_properties
        )

    def shard(self, tensor: torch.Tensor, src_rank: int = 0, process_group=None) -> "ShardedTensor":
        # TODO: figure out a generic and efficient way to scatter the shards for EnumerableShardingSpec
        raise NotImplementedError("EnumerableShardingSpec.shard not implemented yet!")
