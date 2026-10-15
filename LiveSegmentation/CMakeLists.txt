set(MODULE_NAME LiveSegmentation)

set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  LiveSegmentationLib/__init__.py
  LiveSegmentationLib/collaboration.py
  LiveSegmentationLib/features.py
  LiveSegmentationLib/work_log.py
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
