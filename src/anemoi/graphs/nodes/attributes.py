# (C) Copyright 2024 Anemoi contributors.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
#
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

from __future__ import annotations

import logging
from abc import ABC
from abc import abstractmethod
from typing import Type

import numpy as np
import torch
from anemoi.datasets import open_dataset
from scipy.spatial import SphericalVoronoi
from torch_geometric.data import HeteroData
from torch_geometric.data.storage import NodeStorage

from anemoi.graphs.generate.transforms import latlon_rad_to_cartesian
from anemoi.graphs.normalise import NormaliserMixin

LOGGER = logging.getLogger(__name__)


class BaseNodeAttribute(ABC, NormaliserMixin):
    """Base class for the weights of the nodes."""

    def __init__(self, norm: str | None = None, dtype: str = "float32") -> None:
        self.norm = norm
        self.dtype = dtype

    @abstractmethod
    def get_raw_values(self, nodes: NodeStorage, **kwargs) -> np.ndarray: ...

    def post_process(self, values: np.ndarray) -> torch.Tensor:
        """Post-process the values."""
        if values.ndim == 1:
            values = values[:, np.newaxis]

        norm_values = self.normalise(values)

        return torch.tensor(norm_values.astype(self.dtype))

    def compute(self, graph: HeteroData, nodes_name: str, **kwargs) -> torch.Tensor:
        """Get the nodes attribute.

        Parameters
        ----------
        graph : HeteroData
            Graph.
        nodes_name : str
            Name of the nodes.
        kwargs : dict
            Additional keyword arguments.

        Returns
        -------
        torch.Tensor
            Attributes associated to the nodes.
        """
        nodes = graph[nodes_name]
        attributes = self.get_raw_values(nodes, **kwargs)
        return self.post_process(attributes)


class UniformWeights(BaseNodeAttribute):
    """Implements a uniform weight for the nodes.

    Methods
    -------
    compute(self, graph, nodes_name)
        Compute the area attributes for each node.
    """

    def get_raw_values(self, nodes: NodeStorage, **kwargs) -> np.ndarray:
        """Compute the weights.

        Parameters
        ----------
        nodes : NodeStorage
            Nodes of the graph.
        kwargs : dict
            Additional keyword arguments.

        Returns
        -------
        np.ndarray
            Attributes.
        """
        return np.ones(nodes.num_nodes)


class AreaWeights(BaseNodeAttribute):
    """Implements the area of the nodes as the weights.

    Attributes
    ----------
    norm : str
        Normalisation of the weights.
    radius : float
        Radius of the sphere.
    centre : np.ndarray
        Centre of the sphere.

    Methods
    -------
    compute(self, graph, nodes_name)
        Compute the area attributes for each node.
    """

    def __init__(
        self,
        norm: str | None = None,
        radius: float = 1.0,
        centre: np.ndarray = np.array([0, 0, 0]),
        dtype: str = "float32",
    ) -> None:
        super().__init__(norm, dtype)
        self.radius = radius
        self.centre = centre

    def get_raw_values(self, nodes: NodeStorage, **kwargs) -> np.ndarray:
        """Compute the area associated to each node.

        It uses Voronoi diagrams to compute the area of each node.

        Parameters
        ----------
        nodes : NodeStorage
            Nodes of the graph.
        kwargs : dict
            Additional keyword arguments.

        Returns
        -------
        np.ndarray
            Attributes.
        """
        latitudes, longitudes = nodes.x[:, 0], nodes.x[:, 1]
        points = latlon_rad_to_cartesian((np.asarray(latitudes), np.asarray(longitudes)))
        sv = SphericalVoronoi(points, self.radius, self.centre)
        area_weights = sv.calculate_areas()
        LOGGER.debug(
            "There are %d of weights, which (unscaled) add up a total weight of %.2f.",
            len(area_weights),
            np.array(area_weights).sum(),
        )
        return area_weights


class BooleanBaseNodeAttribute(BaseNodeAttribute, ABC):
    """Base class for boolean node attributes."""

    def __init__(self) -> None:
        super().__init__(norm=None, dtype="bool")


class NonmissingZarrVariable(BooleanBaseNodeAttribute):
    """Mask of valid (not missing) values of a Zarr dataset variable.

    It reads a variable from a Zarr dataset and returns a boolean mask of nonmissing values in the first timestep.

    Attributes
    ----------
    variable : str
        Variable to read from the Zarr dataset.
    norm : str
        Normalization of the weights.

    Methods
    -------
    compute(self, graph, nodes_name)
        Compute the attribute for each node.
    """

    def __init__(self, variable: str) -> None:
        super().__init__()
        self.variable = variable

    def get_raw_values(self, nodes: NodeStorage, **kwargs) -> np.ndarray:
        assert (
            nodes["node_type"] == "ZarrDatasetNodes"
        ), f"{self.__class__.__name__} can only be used with ZarrDatasetNodes."
        ds = open_dataset(nodes["_dataset"], select=self.variable)[0].squeeze()
        return ~np.isnan(ds)


class CutOutMask(BooleanBaseNodeAttribute):
    """Cut out mask."""

    def get_raw_values(self, nodes: NodeStorage, **kwargs) -> np.ndarray:
        assert isinstance(nodes["_dataset"], dict), "The 'dataset' attribute must be a dictionary."
        assert "cutout" in nodes["_dataset"], "The 'dataset' attribute must contain a 'cutout' key."
        num_lam, num_other = open_dataset(nodes["_dataset"]).grids
        return np.array([True] * num_lam + [False] * num_other, dtype=bool)


class BooleanOperation(BooleanBaseNodeAttribute, ABC):
    """Base class for boolean operations."""

    def __init__(self, masks: list[str | Type[BooleanBaseNodeAttribute]]) -> None:
        super().__init__()
        self.masks = masks

    @staticmethod
    def get_mask_values(mask: str | Type[BaseNodeAttribute], nodes: NodeStorage, **kwargs) -> np.array:
        if isinstance(mask, str):
            attributes = nodes[mask]
            assert (
                attributes.dtype == "bool"
            ), f"The mask attribute '{mask}' must be a boolean but is {attributes.dtype}."
            return attributes

        return mask.get_raw_values(nodes, **kwargs)


class BooleanAndMask(BooleanOperation):
    """Boolean AND mask."""

    def get_raw_values(self, nodes: NodeStorage, **kwargs) -> np.ndarray:
        return np.logical_and.reduce([BooleanOperation.get_mask_values(mask, nodes, **kwargs) for mask in self.masks])


class BooleanOrMask(BooleanOperation):
    """Boolean OR mask."""

    def get_raw_values(self, nodes: NodeStorage, **kwargs) -> np.ndarray:
        return np.logical_or.reduce([BooleanOperation.get_mask_values(mask, nodes, **kwargs) for mask in self.masks])
