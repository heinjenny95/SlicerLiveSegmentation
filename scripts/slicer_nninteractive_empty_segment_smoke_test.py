import json
import os

import numpy as np
import slicer


def main():
    import SlicerNNInteractiveIR

    volume = slicer.mrmlScene.AddNewNodeByClass(
        "vtkMRMLScalarVolumeNode", "nnInteractive empty-label smoke volume"
    )
    image = np.zeros((5, 6, 7), dtype=np.int16)
    slicer.util.updateVolumeFromArray(volume, image)
    segmentation = slicer.mrmlScene.AddNewNodeByClass(
        "vtkMRMLSegmentationNode", "nnInteractive empty-label smoke segmentation"
    )
    segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
    segment_id = segmentation.GetSegmentation().AddEmptySegment("Fresh live label")
    widget = object.__new__(SlicerNNInteractiveIR.SlicerNNInteractiveIRWidget)
    widget.scribble_segment_node_name = "ScribbleSegmentNode (do not touch)"
    mask = widget.segment_mask_in_reference_geometry(
        segmentation, segment_id, volume, image.shape
    )
    if tuple(mask.shape) != tuple(image.shape) or np.any(mask):
        raise RuntimeError("Fresh empty label did not resolve to a safe zero mask")

    bounds = ((1, 4), (1, 5), (2, 6))
    full_mask = np.zeros(image.shape, dtype=np.uint8)
    full_mask[1:3, 2:4, 3:5] = 1
    slicer.util.updateSegmentBinaryLabelmapFromArray(
        full_mask, segmentation, segment_id, volume
    )
    widget.get_volume_node = lambda: volume
    widget.get_active_crop_bounds = lambda: bounds
    crop_reference = widget.get_or_create_crop_reference_volume_node(bounds)
    cropped = widget.segment_mask_in_reference_geometry(
        segmentation,
        segment_id,
        crop_reference,
        widget.crop_bounds_shape(bounds),
    )
    expected = full_mask[1:4, 1:5, 2:6]
    if not np.array_equal(cropped, expected):
        raise RuntimeError("Direct ROI labelmap read did not preserve source voxels")

    output_path = os.environ.get("NNINTERACTIVE_EMPTY_SEGMENT_SMOKE_OUTPUT")
    if output_path:
        with open(output_path, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "status": "ok",
                    "plugin_version": SlicerNNInteractiveIR.PLUGIN_VERSION,
                    "shape": list(mask.shape),
                    "nonzero_voxels": int(np.count_nonzero(mask)),
                    "roi_nonzero_voxels": int(np.count_nonzero(cropped)),
                },
                stream,
                indent=2,
            )


exit_code = 0
try:
    main()
except Exception:
    import traceback

    traceback.print_exc()
    exit_code = 1
slicer.app.exit(exit_code)
