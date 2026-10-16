"""Bounded nearest-neighbour label sampling, independent of reference image size."""
from __future__ import annotations

import itertools

import numpy as np


def _matrix(value):
    matrix = np.asarray(value, dtype=np.float64)
    if (matrix.shape != (4, 4) or not np.all(np.isfinite(matrix))
            or not np.allclose(matrix[3], [0, 0, 0, 1], rtol=0, atol=1e-10)
            or abs(np.linalg.det(matrix[:3, :3])) < 1e-15):
        raise ValueError("Live label geometry must be a finite invertible affine matrix")
    return matrix


def integer_offset(matrix):
    """Reference IJK -> label IJK translation for an identical voxel lattice."""
    if np.allclose(matrix[:3, :3], np.eye(3), rtol=0, atol=1e-8):
        offset = np.rint(matrix[:3, 3])
        if np.allclose(matrix[:3, 3], offset, rtol=0, atol=1e-5):
            return offset.astype(np.int64)
    return None


def reference_bounds(extent, reference_to_label, shape):
    """Conservative reference-centre bounds of a label image, clipped to source."""
    matrix = _matrix(reference_to_label)
    extent = [int(value) for value in extent]
    if any(extent[2*axis] > extent[2*axis+1] for axis in range(3)):
        return None
    offset = integer_offset(matrix)
    if offset is not None:
        low = np.array(extent[::2]) - offset
        high = np.array(extent[1::2]) + 1 - offset
    else:
        # Cell edges, not only centres: nearest-neighbour support may extend
        # beyond the transformed extreme voxel centres (rotation/scale).
        corners = np.array([(*point, 1.) for point in itertools.product(
            *[(extent[2*a]-0.5, extent[2*a+1]+0.5) for a in range(3)])])
        points = (np.linalg.inv(matrix) @ corners.T).T[:, :3]
        low = np.floor(points.min(axis=0)).astype(np.int64) - 1
        high = np.ceil(points.max(axis=0)).astype(np.int64) + 1
    dims = np.array(tuple(shape)[::-1], dtype=np.int64)
    low, high = np.maximum(low, 0), np.minimum(high, dims)
    if np.any(high <= low):
        return None
    return [int(low[2]), int(high[2]), int(low[1]), int(high[1]), int(low[0]), int(high[0])]


def sample_region(source, extent, reference_to_label, bounds, label_value):
    """Read only requested reference voxels; never resample a full reference.

    Packed siblings keep their native scalar values; compare with this segment's
    label value only. Affine scratch arrays are capped at 65536 samples, including
    when a caller requests a large crop. Source is an unmodified native view.
    """
    matrix = _matrix(reference_to_label)
    source = np.asarray(source)
    extent = np.asarray(extent, dtype=np.int64)
    z0, z1, y0, y1, x0, x1 = [int(value) for value in bounds]
    shape = (z1-z0, y1-y0, x1-x0)
    if source.ndim != 3 or any(size < 0 for size in shape):
        raise ValueError("Invalid live label sampling bounds")
    if tuple((extent[1::2]-extent[::2]+1)[::-1]) != source.shape:
        raise ValueError("Native live label extent does not match its scalar buffer")
    result = np.zeros(shape, dtype=np.uint8)
    offset = integer_offset(matrix)
    if offset is not None:
        low = np.maximum([x0, y0, z0], extent[::2]-offset)
        high = np.minimum([x1, y1, z1], extent[1::2]+1-offset)
        if np.all(high > low):
            target = tuple(slice(int(a-b), int(c-b)) for a, c, b in zip(low[::-1], high[::-1], [z0, y0, x0]))
            native_low, native_high = low+offset-extent[::2], high+offset-extent[::2]
            native = tuple(slice(int(a), int(b)) for a, b in zip(native_low[::-1], native_high[::-1]))
            np.equal(source[native], label_value, out=result[target])
        return result
    flat = result.reshape(-1)
    plane = shape[1]*shape[2]
    for start in range(0, flat.size, 65536):
        indices = np.arange(start, min(flat.size, start+65536), dtype=np.int64)
        z = indices // plane + z0
        y = (indices // shape[2]) % shape[1] + y0
        x = indices % shape[2] + x0
        coords = [np.floor(matrix[a, 0]*x + matrix[a, 1]*y + matrix[a, 2]*z
                           + matrix[a, 3] + 0.5 + 1e-10).astype(np.int64) - extent[2*a]
                  for a in range(3)]
        xi, yi, zi = coords
        valid = ((xi >= 0) & (xi < source.shape[2]) & (yi >= 0) & (yi < source.shape[1])
                 & (zi >= 0) & (zi < source.shape[0]))
        flat[start:start+indices.size][valid] = source[zi[valid], yi[valid], xi[valid]] == label_value
    return result
