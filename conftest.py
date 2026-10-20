"""Root conftest.

Keeps the repo root on sys.path so ``import custom_components`` resolves to
this repository's ``custom_components/person_tracker`` under test, as
expected by the ``pytest-homeassistant-custom-component`` harness plugin
(which supplies the ``hass`` fixture and friends via its pytest entry
point).
"""
