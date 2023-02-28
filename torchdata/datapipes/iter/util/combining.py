# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import warnings

from collections import OrderedDict
from typing import Callable, Iterator, List, Optional, Sequence, TypeVar

from torch.utils.data import functional_datapipe, IterDataPipe, MapDataPipe
from torch.utils.data.datapipes.iter.combining import _ChildDataPipe, _DemultiplexerIterDataPipe, _ForkerIterDataPipe
from torch.utils.data.datapipes.utils.common import _check_unpickable_fn

from torchdata.datapipes.utils.janitor import janitor

T_co = TypeVar("T_co", covariant=True)
T = TypeVar("T")


@functional_datapipe("zip_with_iter")
class IterKeyZipperIterDataPipe(IterDataPipe[T_co]):
    r"""
    Zips two IterDataPipes together based on the matching key (functional name: ``zip_with_iter``). The keys
    are computed by ``key_fn`` and ``ref_key_fn`` for the two IterDataPipes, respectively. When there isn't a match
    between the elements of the two IterDataPipes, the element from ``ref_datapipe`` is stored in a buffer. Then, the
    next element from ``ref_datapipe`` is tried. After a match is found, the ``merge_fn`` determines how they will
    be combined and returned (a tuple is generated by default).

    Args:
        source_datapipe: IterKeyZipper will yield data based on the order of this IterDataPipe
        ref_datapipe: Reference IterDataPipe from which IterKeyZipper will find items
            with matching key for ``source_datapipe``
        key_fn: Callable function that will compute keys using elements from ``source_datapipe``
        ref_key_fn: Callable function that will compute keys using elements from ``ref_datapipe``
            If it's not specified, the ``key_fn`` will also be applied to elements from ``ref_datapipe``
        keep_key: Option to yield the matching key along with the items in a tuple,
            resulting in `(key, merge_fn(item1, item2))`.
        buffer_size: The size of buffer used to hold key-data pairs from reference DataPipe until a match is found.
            If it's specified as ``None``, the buffer size is set as infinite.
        merge_fn: Function that combines the item from ``source_datapipe`` and the item from ``ref_datapipe``,
            by default a tuple is created

    Example:
        >>> from torchdata.datapipes.iter import IterableWrapper
        >>> from operator import itemgetter
        >>> def merge_fn(t1, t2):
        >>>     return t1[1] + t2[1]
        >>> dp1 = IterableWrapper([('a', 100), ('b', 200), ('c', 300)])
        >>> dp2 = IterableWrapper([('a', 1), ('b', 2), ('c', 3), ('d', 4)])
        >>> res_dp = dp1.zip_with_iter(dp2, key_fn=itemgetter(0),
        >>>                            ref_key_fn=itemgetter(0), keep_key=True, merge_fn=merge_fn)
        >>> list(res_dp)
        [('a', 101), ('b', 202), ('c', 303)]
    """

    def __init__(
        self,
        source_datapipe: IterDataPipe,
        ref_datapipe: IterDataPipe,
        key_fn: Callable,
        ref_key_fn: Optional[Callable] = None,
        keep_key: bool = False,
        buffer_size: int = 10000,
        merge_fn: Optional[Callable] = None,
    ) -> None:
        if not isinstance(ref_datapipe, IterDataPipe):
            raise TypeError(f"ref_datapipe must be a IterDataPipe, but its type is {type(ref_datapipe)} instead.")
        self.source_datapipe = source_datapipe
        self.ref_datapipe = ref_datapipe
        _check_unpickable_fn(key_fn)
        self.key_fn = key_fn
        if ref_key_fn is not None:
            _check_unpickable_fn(ref_key_fn)
        self.ref_key_fn = key_fn if ref_key_fn is None else ref_key_fn
        self.keep_key = keep_key
        if merge_fn is not None:
            _check_unpickable_fn(merge_fn)
        self.merge_fn = merge_fn
        if buffer_size is not None and buffer_size <= 0:
            raise ValueError("'buffer_size' is required to be either None or a positive integer.")
        self.buffer_size: int = buffer_size
        self.buffer: OrderedDict = OrderedDict()

    def __iter__(self) -> Iterator:
        ref_it = iter(self.ref_datapipe)
        warn_once_flag = True
        try:
            for data in self.source_datapipe:
                key = self.key_fn(data)
                while key not in self.buffer:
                    try:
                        ref_data = next(ref_it)
                    except StopIteration:
                        raise BufferError(
                            f"No matching key can be found from reference DataPipe for the data {data}. "
                            "Please consider increasing the buffer size."
                        )
                    ref_key = self.ref_key_fn(ref_data)
                    if ref_key in self.buffer:
                        raise ValueError("Duplicate key is found in reference DataPipe")
                    if self.buffer_size is not None and len(self.buffer) > self.buffer_size:
                        if warn_once_flag:
                            warn_once_flag = False
                            warnings.warn(
                                "Buffer reaches the upper limit, so reference key-data pair begins to "
                                "be removed from buffer in FIFO order. Please consider increase buffer size."
                            )
                        self.buffer.popitem(last=False)
                    self.buffer[ref_key] = ref_data
                res = self.merge_fn(data, self.buffer.pop(key)) if self.merge_fn else (data, self.buffer.pop(key))
                if self.keep_key:
                    yield key, res
                else:
                    yield res
        finally:
            del ref_it
            # TODO(633): This should be Exception or warn when debug mode is enabled
            if self.buffer:
                for _, v in self.buffer.items():
                    janitor(v)
                self.buffer.clear()

    def __len__(self) -> int:
        return len(self.source_datapipe)

    def reset(self) -> None:
        self.buffer = OrderedDict()

    def __getstate__(self):
        state = (
            self.source_datapipe,
            self.ref_datapipe,
            self.key_fn,
            self.ref_key_fn,
            self.keep_key,
            self.merge_fn,
            self.buffer_size,
        )
        if IterDataPipe.getstate_hook is not None:
            return IterDataPipe.getstate_hook(state)
        return state

    def __setstate__(self, state):
        (
            self.source_datapipe,
            self.ref_datapipe,
            self.key_fn,
            self.ref_key_fn,
            self.keep_key,
            self.merge_fn,
            self.buffer_size,
        ) = state
        self.buffer = OrderedDict()

    def __del__(self):
        if self.buffer:
            for _, v in self.buffer.items():
                janitor(v)
            self.buffer.clear()


@functional_datapipe("zip_with_map")
class MapKeyZipperIterDataPipe(IterDataPipe[T_co]):
    r"""
    Joins the items from the source IterDataPipe with items from a MapDataPipe (functional name: ``zip_with_map``).
    The matching is done by the provided ``key_fn``, which maps an item from ``source_iterdatapipe`` to
    a key that should exist in the ``map_datapipe``. The return value is created by the ``merge_fn``, which returns
    a tuple of the two items by default.

    Args:
        source_iterdatapipe: IterDataPipe from which items are yield and will be combined with an item
            from ``map_datapipe``
        map_datapipe: MapDataPipe that takes a key from ``key_fn``, and returns an item
        key_fn: Function that maps each item from ``source_iterdatapipe`` to a key that exists in ``map_datapipe``
        keep_key: Option to yield the matching key along with the items in a tuple,
            resulting in ``(key, merge_fn(item1, item2))``.
        merge_fn: Function that combines the item from ``source_iterdatapipe`` and the matching item
            from ``map_datapipe``, by default a tuple is created

    Example:

    .. testsetup::

        from operator import itemgetter

    .. testcode::

        from torchdata.datapipes.iter import IterableWrapper
        from torchdata.datapipes.map import SequenceWrapper

        def merge_fn(tuple_from_iter, value_from_map):
            return tuple_from_iter[0], tuple_from_iter[1] + value_from_map
        dp1 = IterableWrapper([('a', 1), ('b', 2), ('c', 3)])
        mapdp = SequenceWrapper({'a': 100, 'b': 200, 'c': 300, 'd': 400})
        res_dp = dp1.zip_with_map(map_datapipe=mapdp, key_fn=itemgetter(0), merge_fn=merge_fn)
        print(list(res_dp))

    .. testoutput::

        [('a', 101), ('b', 202), ('c', 303)]

    """

    def __init__(
        self,
        source_iterdatapipe: IterDataPipe,
        map_datapipe: MapDataPipe,
        key_fn: Callable,
        merge_fn: Optional[Callable] = None,
        keep_key: bool = False,
    ):
        if not isinstance(map_datapipe, MapDataPipe):
            raise TypeError(f"map_datapipe must be a MapDataPipe, but its type is {type(map_datapipe)} instead.")
        self.source_iterdatapipe: IterDataPipe = source_iterdatapipe
        self.map_datapipe: MapDataPipe = map_datapipe
        _check_unpickable_fn(key_fn)
        self.key_fn: Callable = key_fn
        if merge_fn is not None:
            _check_unpickable_fn(merge_fn)
        self.merge_fn: Optional[Callable] = merge_fn
        self.keep_key = keep_key

    def __iter__(self) -> Iterator:
        for item in self.source_iterdatapipe:
            key = self.key_fn(item)
            try:
                map_item = self.map_datapipe[key]
            except (KeyError, IndexError):
                raise KeyError(f"key_fn maps {item} to {key}, which is not a valid key in the given MapDataPipe.")
            res = self.merge_fn(item, map_item) if self.merge_fn else (item, map_item)
            if self.keep_key:
                yield key, res
            else:
                yield res

    def __len__(self) -> int:
        return len(self.source_iterdatapipe)


def _drop_index(idx_data):
    _, data = idx_data
    return data


@functional_datapipe("round_robin_demux")
class RoundRobinDemultiplexerIterDataPipe(IterDataPipe):
    r"""
    Splits the input DataPipe into multiple child DataPipes in the round-robin order (functional name: ``round_robin_demux``).
    A list of the child DataPipes is returned from this operation.

    Args:
        datapipe: Iterable DataPipe being filtered
        num_instances: number of instances of the DataPipe to create
        buffer_size: this defines the maximum number of inputs that the buffer can hold across all child
            DataPipes while waiting for their values to be yielded.
            Defaults to ``1000``. Use ``-1`` for the unlimited buffer.

    Examples:
        >>> from torchdata.datapipes.iter import IterableWrapper
        >>> source_dp = IterableWrapper(range(5))
        >>> dp1, dp2 = source_dp.round_robin_demux(2)
        >>> list(dp1)
        [0, 2, 4]
        >>> len(dp1)
        3
        >>> list(dp2)
        [1, 3]
        >>> len(dp2)
        2
    """

    def __new__(cls, datapipe: IterDataPipe, num_instances: int, buffer_size: int = 1000):
        if num_instances < 1:
            raise ValueError(f"Expected `num_instaces` larger than 0, but {num_instances} is found")
        if num_instances == 1:
            warnings.warn(
                "The operation of `round_robin_demux` with `num_instances=1` is an no-op and returns the provided `datapipe` in a list directly"
            )
            return [datapipe]

        datapipe = datapipe.enumerate()
        container = _RoundRobinDemultiplexerIterDataPipe(datapipe, num_instances, buffer_size=buffer_size)
        return [_ChildDataPipe(container, i).map(_drop_index) for i in range(num_instances)]


class _RoundRobinDemultiplexerIterDataPipe(_DemultiplexerIterDataPipe):
    def __init__(self, datapipe: IterDataPipe[T_co], num_instances: int, buffer_size: int):
        super().__init__(datapipe, num_instances, self._round_robin_fn, drop_none=False, buffer_size=buffer_size)

    def _round_robin_fn(self, idx_data) -> int:
        idx, _ = idx_data
        return idx % self.num_instances

    def get_length_by_instance(self, instance_id: int) -> int:
        n = len(self.main_datapipe)
        avg_length = n // self.num_instances
        return avg_length + 1 if n - avg_length * self.num_instances > instance_id else avg_length


@functional_datapipe("unzip")
class UnZipperIterDataPipe(IterDataPipe[T]):
    r"""
    Takes in a DataPipe of Sequences, unpacks each Sequence, and return the elements in separate DataPipes
    based on their position in the Sequence (functional name: ``unzip``). The number of instances produced equals to
    the sequence length minus the number of columns to skip.

    Note:
        Each sequence within the DataPipe should have the same length, specified by
        the input argument `sequence_length`.

    Args:
        source_datapipe: Iterable DataPipe with sequences of data
        sequence_length: Length of the sequence within the source_datapipe. All elements should have the same length.
        buffer_size: this restricts how far ahead the leading child DataPipe can read relative
            to the slowest child DataPipe. Use -1 for the unlimited buffer.
        columns_to_skip: optional indices of columns that the DataPipe should skip (each index should be
            an integer from 0 to sequence_length - 1)

    Example:
        >>> from torchdata.datapipes.iter import IterableWrapper
        >>> source_dp = IterableWrapper([(i, i + 10, i + 20) for i in range(3)])
        >>> dp1, dp2, dp3 = source_dp.unzip(sequence_length=3)
        >>> list(dp1)
        [0, 1, 2]
        >>> list(dp2)
        [10, 11, 12]
        >>> list(dp3)
        [20, 21, 22]
    """

    def __new__(
        cls,
        source_datapipe: IterDataPipe[Sequence[T]],
        sequence_length: int,
        buffer_size: int = 1000,
        columns_to_skip: Optional[Sequence[int]] = None,
    ):
        if columns_to_skip is None:
            instance_ids = list(range(sequence_length))
        else:
            skips = set(columns_to_skip)
            instance_ids = [i for i in range(sequence_length) if i not in skips]

        if len(instance_ids) == 0:
            raise RuntimeError(
                "All instances are being filtered out in UnZipperIterDataPipe. Please check"
                "the input `sequence_length` and `columns_to_skip`."
            )

        # The implementation basically uses Forker but only yields a specific element within the sequence
        container = _UnZipperIterDataPipe(source_datapipe, instance_ids, buffer_size)  # type: ignore[arg-type]
        return [_ChildDataPipe(container, i) for i in range(len(instance_ids))]


class _UnZipperIterDataPipe(_ForkerIterDataPipe):
    def __init__(self, datapipe: IterDataPipe, instance_ids: List[int], buffer_size: int = 1000):
        super().__init__(datapipe, len(instance_ids), buffer_size)  # type: ignore[arg-type]
        self.instance_ids = instance_ids

    def get_next_element_by_instance(self, instance_id: int):
        r"""
        Note:
            Each element returned from the source datapipe is required to be a sequnce that can
            be subscribed with a column index
        """
        for return_val in super().get_next_element_by_instance(instance_id):
            yield return_val[self.instance_ids[instance_id]]

    def __getstate__(self):
        state = super().__getstate__()
        return (*state, self.instance_ids)

    def __setstate__(self, state):
        super().__setstate__(state[:-1])
        self.instance_ids = state[-1]
