set(MODULE_NAME LiveSegmentation)

set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  LiveSegmentationLib/__init__.py
  LiveSegmentationLib/collaboration.py
  LiveSegmentationLib/features.py
)

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.svg
)

slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
)
