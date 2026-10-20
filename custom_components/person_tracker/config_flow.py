"""Config flow person_tracker integration."""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    ABORT_SINGLE_INSTANCE,
    CONF_ADD_CAMERA,
    CONF_ADD_PERSON,
    CONF_AI_TASK_ENTITY,
    CONF_AUTHORITATIVE_TRACKER,
    CONF_CAMERA,
    CONF_CAMERAS,
    CONF_PEOPLE,
    CONF_PERSON_NAME,
    CONF_TTL,
    CONF_TRIGGERS,
    DEFAULT_TTL_MINUTES,
    DOMAIN,
    ERR_CAMERA_WITHOUT_AREA,
    ERR_DUPLICATE_CAMERA,
    ERR_INVALID_AI_TASK_ENTITY,
    ERR_INVALID_AUTHORITATIVE_TRACKER,
    ERR_INVALID_CAMERA,
    ERR_INVALID_PERSON_NAME,
    ERR_INVALID_TRIGGER,
    ERR_NO_AI_TASK_ENTITY,
    ERR_NO_TRIGGERS,
    ERR_UNIQUE_PERSON,
)
from .helpers import (
    get_default_ai_task_entity_id,
    is_camera_in_area,
    is_valid_person_name,
    is_usable_ai_task_entity,
)
from .models import person_slug


def _get_person_schema(
    *, name: str | None = None, ttl: int = DEFAULT_TTL_MINUTES
) -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_PERSON_NAME, default=name): TextSelector(),
            probatio.Optional(CONF_TTL, default=ttl): NumberSelector(
                NumberSelectorConfig(min=1, max=1440, mode=NumberSelectorMode.BOX)
            ),
            probatio.Optional(CONF_AUTHORITATIVE_TRACKER): EntitySelector(
                EntitySelectorConfig(domain="device_tracker")
            ),
        }
    )


def _get_camera_schema() -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_CAMERA): EntitySelector(
                EntitySelectorConfig(domain="camera")
            ),
            probatio.Required(CONF_TRIGGERS): EntitySelector(
                EntitySelectorConfig(domain="binary_sensor", multiple=True)
            ),
        }
    )


_PERSON_MORE_SCHEMA = probatio.Schema(
    {probatio.Required(CONF_ADD_PERSON): BooleanSelector()}
)
_CAMERA_MORE_SCHEMA = probatio.Schema(
    {probatio.Required(CONF_ADD_CAMERA): BooleanSelector()}
)
_AI_TASK_SCHEMA = probatio.Schema(
    {
        probatio.Optional(CONF_AI_TASK_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="ai_task")
        )
    }
)


class PersonTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Person Tracker."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow and per-flow scratch state."""
        super().__init__()
        self._people: list[dict[str, Any]] = []
        self._cameras: list[dict[str, Any]] = []

    def _domain_of(self, entity_id: str) -> str | None:
        """Return the domain of *entity_id*'s state, or None if unseen."""
        return (state := self.hass.states.get(entity_id)) and state.domain

    def _build_person(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Validate *user_input* and build a person record.

        Returns (person, errors); *person* is empty when *errors* is set.
        """
        name = user_input[CONF_PERSON_NAME]
        errors: dict[str, str] = {}
        authoritative = user_input.get(CONF_AUTHORITATIVE_TRACKER)
        if not is_valid_person_name(name):
            errors[CONF_PERSON_NAME] = ERR_INVALID_PERSON_NAME
        elif any(
            person_slug(p[CONF_PERSON_NAME]) == person_slug(name) for p in self._people
        ):
            # Names that differ only in case or punctuation share a slug, which
            # is the entity unique_id key.
            errors[CONF_PERSON_NAME] = ERR_UNIQUE_PERSON
        elif authoritative and self._domain_of(authoritative) != "device_tracker":
            errors[CONF_AUTHORITATIVE_TRACKER] = ERR_INVALID_AUTHORITATIVE_TRACKER
        if errors:
            return {}, errors
        person: dict[str, Any] = {
            CONF_PERSON_NAME: name,
            CONF_TTL: user_input[CONF_TTL],
        }
        if authoritative:
            person[CONF_AUTHORITATIVE_TRACKER] = authoritative
        return person, {}

    def _build_camera(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Validate *user_input* and build a camera record.

        Returns (camera, errors); *camera* is empty when *errors* is set.
        """
        camera_id = user_input[CONF_CAMERA]
        triggers = user_input.get(CONF_TRIGGERS) or []
        errors: dict[str, str] = {}
        if self._domain_of(camera_id) != "camera":
            errors[CONF_CAMERA] = ERR_INVALID_CAMERA
        elif not is_camera_in_area(self.hass, camera_id):
            errors[CONF_CAMERA] = ERR_CAMERA_WITHOUT_AREA
        elif any(c[CONF_CAMERA] == camera_id for c in self._cameras):
            errors[CONF_CAMERA] = ERR_DUPLICATE_CAMERA
        elif not triggers:
            errors[CONF_TRIGGERS] = ERR_NO_TRIGGERS
        elif any(self._domain_of(trigger) != "binary_sensor" for trigger in triggers):
            errors[CONF_TRIGGERS] = ERR_INVALID_TRIGGER
        if errors:
            return {}, errors
        return {CONF_CAMERA: camera_id, CONF_TRIGGERS: triggers}, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User-initiated setup: first person."""
        if self._async_current_entries():
            return self.async_abort(reason=ABORT_SINGLE_INSTANCE)
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=_get_person_schema()
            )

        person, errors = self._build_person(user_input)
        if errors:
            return self.async_show_form(
                step_id="user",
                data_schema=_get_person_schema(
                    name=user_input.get(CONF_PERSON_NAME),
                    ttl=user_input.get(CONF_TTL, DEFAULT_TTL_MINUTES),
                ),
                errors=errors,
            )

        self._people.append(person)
        return await self.async_step_person_more()

    async def async_step_person_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add another person."""
        if user_input is None:
            return self.async_show_form(
                step_id="person_add", data_schema=_get_person_schema()
            )

        person, errors = self._build_person(user_input)
        if errors:
            return self.async_show_form(
                step_id="person_add",
                data_schema=_get_person_schema(
                    name=user_input.get(CONF_PERSON_NAME),
                    ttl=user_input.get(CONF_TTL, DEFAULT_TTL_MINUTES),
                ),
                errors=errors,
            )

        self._people.append(person)
        return await self.async_step_person_more()

    async def async_step_person_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask to add another person."""
        if user_input is None:
            return self.async_show_form(
                step_id="person_more",
                data_schema=_PERSON_MORE_SCHEMA,
            )

        if user_input[CONF_ADD_PERSON]:
            return await self.async_step_person_add()
        return await self.async_step_camera_first()

    async def async_step_camera_first(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First camera setup."""
        if user_input is None:
            return self.async_show_form(
                step_id="camera_first", data_schema=_get_camera_schema()
            )

        camera, errors = self._build_camera(user_input)
        if errors:
            return self.async_show_form(
                step_id="camera_first",
                data_schema=_get_camera_schema(),
                errors=errors,
            )

        self._cameras.append(camera)
        return await self.async_step_cameras_more()

    async def async_step_cameras_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask to add another camera."""
        if user_input is None:
            return self.async_show_form(
                step_id="cameras_more",
                data_schema=_CAMERA_MORE_SCHEMA,
            )

        if user_input[CONF_ADD_CAMERA]:
            return await self.async_step_camera_add()
        return await self.async_step_ai_task()

    async def async_step_camera_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add another camera."""
        if user_input is None:
            return self.async_show_form(
                step_id="camera_add", data_schema=_get_camera_schema()
            )

        camera, errors = self._build_camera(user_input)
        if errors:
            return self.async_show_form(
                step_id="camera_add",
                data_schema=_get_camera_schema(),
                errors=errors,
            )

        self._cameras.append(camera)
        return await self.async_step_cameras_more()

    async def async_step_ai_task(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finalize: select AI task."""
        if user_input is None:
            return self.async_show_form(step_id="ai_task", data_schema=_AI_TASK_SCHEMA)

        ai_task_entity = user_input.get(CONF_AI_TASK_ENTITY)
        errors = {}

        if ai_task_entity and not is_usable_ai_task_entity(self.hass, ai_task_entity):
            errors[CONF_AI_TASK_ENTITY] = ERR_INVALID_AI_TASK_ENTITY
        elif not ai_task_entity and not get_default_ai_task_entity_id(self.hass):
            errors["base"] = ERR_NO_AI_TASK_ENTITY

        if errors:
            return self.async_show_form(
                step_id="ai_task", data_schema=_AI_TASK_SCHEMA, errors=errors
            )

        return self.async_create_entry(
            title="Person Tracker",
            data={
                CONF_PEOPLE: self._people,
                CONF_CAMERAS: self._cameras,
                CONF_AI_TASK_ENTITY: ai_task_entity,
            },
        )
