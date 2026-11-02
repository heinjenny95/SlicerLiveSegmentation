set(MODULE_NAME LiveSegmentation)

set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  LiveSegmentationLib/__init__.py
  LiveSegmentationLib/collaboration.py
  LiveSegmentationLib/checkpoints.py
  LiveSegmentationLib/diagnostics.py
  LiveSegmentationLib/features.py
  LiveSegmentationLib/work_log.py
  LiveSegmentationLib/work_time_transfer.py
  LiveSegmentationLib/active_work.py
  LiveSegmentationLib/roi_geometry.py
  LiveSegmentationLib/editor_preprocessing.py
  LiveSegmentationLib/native_range.py
  LiveSegmentationLib/regional_paint.py
  LiveSegmentationLib/view_preferences.py
  LiveSegmentationLib/version.py
  LiveSegmentationLib/ssh_transport.py
)

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.svg
  Resources/Icons/nnInteractiveIR.svg
  Resources/Scripts/OpenLiveSegmentationRoom.py
)

slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
)
