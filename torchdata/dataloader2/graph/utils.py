# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


from collections import deque
from typing import Deque, List, Optional, Set, Type, Union

from torchdata.dataloader2.graph import DataPipe, DataPipeGraph, traverse_dps
from torchdata.datapipes.iter import IterDataPipe
from torchdata.datapipes.map import MapDataPipe


def find_dps(graph: DataPipeGraph, dp_type: Type[DataPipe]) -> List[DataPipe]:
    r"""
    Given the graph of DataPipe generated by ``traverse_dps`` function, return DataPipe
    instances with the provided DataPipe type.
    """
    dps: List[DataPipe] = []
    cache: Set[int] = set()

    def helper(g) -> None:  # pyre-ignore
        for dp_id, (dp, src_graph) in g.items():
            if dp_id in cache:
                continue
            cache.add(dp_id)
            if type(dp) is dp_type:  # Please not use `isinstance`, there is a bug.
                dps.append(dp)
            helper(src_graph)

    helper(graph)

    return dps


def list_dps(graph: DataPipeGraph, exclude_dps: Optional[Union[DataPipe, List[DataPipe]]] = None) -> List[DataPipe]:
    r"""
    Given the graph of DataPipe generated by ``traverse_dps`` function, return a list
    of all DataPipe instances without duplication. If ``exclude_dps`` is provided,
    the provided ``DataPipes`` and their predecessors will be ignored.

    Note:
        - The returned list is in the order of breadth first search of the graph
    """
    dps: List[DataPipe] = []
    cache: Set[int] = set()

    if exclude_dps is not None:
        if isinstance(exclude_dps, (IterDataPipe, MapDataPipe)):
            exclude_dps = [
                exclude_dps,
            ]
        for exclude_dp in exclude_dps:  # type: ignore[union-attr]
            assert isinstance(exclude_dp, (IterDataPipe, MapDataPipe))
            # Skip DataPipe that has already been excluded
            if id(exclude_dp) in cache:
                continue
            for dp in list_dps(traverse_dps(exclude_dp)):  # type: ignore[arg-type]
                cache.add(id(dp))

    q: Deque = deque()
    # Initialization
    for dp_id, (dp, subgraph) in graph.items():
        if dp_id not in cache:
            q.append((dp_id, dp, subgraph))
            cache.add(dp_id)

    while len(q) > 0:
        dp_id, dp, subgraph = q.popleft()
        dps.append(dp)
        for parent_dp_id, (parent_dp, parent_subgraph) in subgraph.items():
            if parent_dp_id not in cache:
                q.append((parent_dp_id, parent_dp, parent_subgraph))
                cache.add(parent_dp_id)

    return dps


# Given the DataPipe needs to be replaced and the expected DataPipe, return a new graph
def replace_dp(graph: DataPipeGraph, old_datapipe: DataPipe, new_datapipe: DataPipe) -> DataPipeGraph:
    r"""
    Given the graph of DataPipe generated by ``traverse_dps`` function and the DataPipe to be replaced and
    the new DataPipe, return the new graph of DataPipe.
    """
    assert len(graph) == 1

    if id(old_datapipe) in graph:
        graph = traverse_dps(new_datapipe)

    final_datapipe = list(graph.values())[0][0]

    for recv_dp, send_graph in graph.values():
        _replace_dp(recv_dp, send_graph, old_datapipe, new_datapipe)

    return traverse_dps(final_datapipe)


def remove_dp(graph: DataPipeGraph, datapipe: DataPipe) -> DataPipeGraph:
    r"""
    Given the graph of DataPipe generated by ``traverse_dps`` function and the DataPipe to be removed,
    return the new graph of DataPipe.

    Note:
        - This function can not remove DataPipe that takes multiple DataPipes as the input.
    """
    assert len(graph) == 1

    dp_graph = traverse_dps(datapipe)
    dp_id = id(datapipe)
    if len(dp_graph[dp_id][1]) == 0:
        raise RuntimeError("Cannot remove the source DataPipe from the graph of DataPipe")
    if len(dp_graph[dp_id][1]) > 1:
        raise RuntimeError("Cannot remove the receiving DataPipe having multiple sending DataPipes")

    if dp_id in graph:
        graph = graph[dp_id][1]

    for recv_dp, send_graph in graph.values():
        _remove_dp(recv_dp, send_graph, datapipe)

    # Get the last DataPipe in graph
    assert len(graph) == 1
    datapipe = list(graph.values())[0][0]

    return traverse_dps(datapipe)


# For each `recv_dp`, find if the source_datapipe needs to be replaced by the new one.
# If found, find where the `old_dp` is located in `recv_dp` and switch it to the `new_dp`
def _replace_dp(recv_dp, send_graph: DataPipeGraph, old_dp: DataPipe, new_dp: DataPipe) -> None:
    old_dp_id = id(old_dp)
    for send_id in send_graph:
        if send_id == old_dp_id:
            _assign_attr(recv_dp, old_dp, new_dp, inner_dp=True)
        else:
            send_dp, sub_send_graph = send_graph[send_id]
            _replace_dp(send_dp, sub_send_graph, old_dp, new_dp)


# For each `recv_dp`, find if the source_datapipe needs to be replaced by the new one.
# If found, find where the `old_dp` is located in `dp` and switch it to the `new_dp`
def _remove_dp(recv_dp, send_graph: DataPipeGraph, datapipe: DataPipe) -> None:
    dp_id = id(datapipe)
    for send_dp_id in send_graph:
        if send_dp_id == dp_id:
            send_dp, sub_send_graph = send_graph[send_dp_id]
            # if len(sub_send_graph) == 0:
            #     raise RuntimeError("Cannot remove the source DataPipe from the graph of DataPipe")
            # if len(sub_send_graph) > 1:
            #     raise RuntimeError("Cannot remove the receiving DataPipe having multiple sending DataPipes")
            src_dp = list(sub_send_graph.values())[0][0]
            _assign_attr(recv_dp, send_dp, src_dp, inner_dp=True)
        else:
            send_dp, sub_send_graph = send_graph[send_dp_id]
            _remove_dp(send_dp, sub_send_graph, datapipe)


# Recursively re-assign datapipe for the sake of nested data structure
# `inner_dp` is used to prevent recursive call if we have already met a `DataPipe`
def _assign_attr(obj, old_dp, new_dp, inner_dp: bool = False):
    if obj is old_dp:
        return new_dp
    elif isinstance(obj, (IterDataPipe, MapDataPipe)):
        # Prevent recursive call for DataPipe
        if not inner_dp:
            return None
        for k in list(obj.__dict__.keys()):
            new_obj = _assign_attr(obj.__dict__[k], old_dp, new_dp)
            if new_obj is not None:
                obj.__dict__[k] = new_obj
                break
        return None
    elif isinstance(obj, dict):
        for k in list(obj.keys()):
            new_obj = _assign_attr(obj[k], old_dp, new_dp)
            if new_obj is not None:
                obj[k] = new_obj
                break
        return None
    # Tuple is immutable, has to re-create a tuple
    elif isinstance(obj, tuple):
        temp_list = []
        flag = False
        for o in obj:
            new_obj = _assign_attr(o, old_dp, new_dp, inner_dp)
            if new_obj is not None:
                flag = True
                temp_list.append(new_dp)
            else:
                temp_list.append(o)
        if flag:
            return tuple(temp_list)  # Special case
        else:
            return None
    elif isinstance(obj, list):
        for i in range(len(obj)):
            new_obj = _assign_attr(obj[i], old_dp, new_dp, inner_dp)
            if new_obj is not None:
                obj[i] = new_obj
                break
        return None
    elif isinstance(obj, set):
        new_obj = None
        for o in obj:
            if _assign_attr(o, old_dp, new_dp, inner_dp) is not None:
                new_obj = new_dp
                break
        if new_obj is not None:
            obj.remove(old_dp)
            obj.add(new_dp)
        return None
    else:
        return None
