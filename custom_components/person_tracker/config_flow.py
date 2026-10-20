"""Config flow for the person_tracker integration.

The config entry holds the global settings (camera->trigger map, optional AI
task entity), set up once and editable via Reconfigure. Each person is a
config subentry, added and edited from the integration page.
"""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
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
    ABORT_NO_AI_TASK_ENTITY,
    ABORT_SINGLE_INSTANCE,
    CONF_ADD_CAMERA,
    CONF_AI_TASK_ENTITY,
    CONF_AUTHORITATIVE_TRACKER,
    CONF_CAMERA,
    CONF_CAMERAS,
    CONF_PERSON_NAME,
    CONF_REMOVE_CAMERAS,
    CONF_TTL,
    CONF_TRIGGERS,
    DEFAULT_TTL_MINUTES,
    DOMAIN,
    ERR_NO_CAMERAS,
    MAX_TTL_MINUTES,
    SUBENTRY_PERSON,
)
from .helpers import get_default_ai_task_entity_id
from .models import person_slug
from .validation import (
    ai_task_error,
    authoritative_tracker_error,
    new_camera_error,
    person_name_error,
    triggers_error,
)

_PERSON_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_PERSON_NAME): TextSelector(),
        probatio.Optional(CONF_TTL, default=DEFAULT_TTL_MINUTES): NumberSelector(
            NumberSelectorConfig(
                min=1, max=MAX_TTL_MINUTES, mode=NumberSelectorMode.BOX
            )
        ),
        probatio.Optional(CONF_AUTHORITATIVE_TRACKER): EntitySelector(
            EntitySelectorConfig(domain="device_tracker")
        ),
    }
)
_TRIGGERS_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="binary_sensor", multiple=True)
)
_CAMERA_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_CAMERA): EntitySelector(
            EntitySelectorConfig(domain="camera")
        ),
        probatio.Required(CONF_TRIGGERS): _TRIGGERS_SELECTOR,
    }
)
_TRIGGERS_SCHEMA = probatio.Schema(
    {probatio.Required(CONF_TRIGGERS): _TRIGGERS_SELECTOR}
)
_CAMERA_MORE_SCHEMA = probatio.Schema(
    {probatio.Required(CONF_ADD_CAMERA): BooleanSelector()}
)
_AI_TASK_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_AI_TASK_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="ai_task")
        )
    }
)


def _configured_cameras_schema(key: str, cameras: list[str]) -> probatio.Schema:
    """Return a schema picking from the configured *cameras*."""
    return probatio.Schema(
        {
            probatio.Required(key): EntitySelector(
                EntitySelectorConfig(
                    include_entities=cameras, multiple=key == CONF_REMOVE_CAMERAS
                )
            )
        }
    )


class PersonTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up and reconfigure the global Person Tracker settings."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow and per-flow scratch state."""
        super().__init__()
        self._cameras: list[dict[str, Any]] = []
        self._edit_camera: str | None = None

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """People are subentries of the entry."""
        return {SUBENTRY_PERSON: PersonSubentryFlow}

    @property
    def _reconfiguring(self) -> bool:
        return self.source == SOURCE_RECONFIGURE

    def _camera_ids(self) -> list[str]:
        return [camera[CONF_CAMERA] for camera in self._cameras]

    def _save(self, **data_updates: Any) -> ConfigFlowResult:
        """Store reconfigured settings and reload the entry."""
        entry = self._get_reconfigure_entry()
        if entry.update_listeners:
            # A loaded entry reloads itself through its update listener.
            return self.async_update_and_abort(entry, data_updates=data_updates)
        return self.async_update_reload_and_abort(entry, data_updates=data_updates)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User-initiated setup: first camera."""
        if self._async_current_entries():
            return self.async_abort(reason=ABORT_SINGLE_INSTANCE)
        if not self.hass.states.async_entity_ids("ai_task"):
            return self.async_abort(reason=ABORT_NO_AI_TASK_ENTITY)
        return await self._async_camera_step("user", user_input)

    async def async_step_cameras_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask to add another camera."""
        if user_input is None:
            return self.async_show_form(
                step_id="cameras_more", data_schema=_CAMERA_MORE_SCHEMA
            )
        if user_input[CONF_ADD_CAMERA]:
            return await self.async_step_camera_add()
        return await self.async_step_ai_task()

    async def async_step_camera_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a camera."""
        return await self._async_camera_step("camera_add", user_input)

    async def _async_camera_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show or handle the add-camera form for *step_id*."""
        errors: dict[str, str] = {}
        if user_input is not None:
            camera_id = user_input[CONF_CAMERA]
            triggers = user_input.get(CONF_TRIGGERS) or []
            if error := new_camera_error(self.hass, camera_id, self._camera_ids()):
                errors[CONF_CAMERA] = error
            elif error := triggers_error(self.hass, triggers):
                errors[CONF_TRIGGERS] = error
            else:
                self._cameras.append({CONF_CAMERA: camera_id, CONF_TRIGGERS: triggers})
                if self._reconfiguring:
                    return self._save(**{CONF_CAMERAS: self._cameras})
                return await self.async_step_cameras_more()
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(_CAMERA_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_ai_task(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the AI task entity, then finish."""
        errors: dict[str, str] = {}
        if user_input is not None:
            ai_task_entity = user_input[CONF_AI_TASK_ENTITY]
            if error := ai_task_error(self.hass, ai_task_entity):
                errors[CONF_AI_TASK_ENTITY] = error
            elif self._reconfiguring:
                return self._save(**{CONF_AI_TASK_ENTITY: ai_task_entity})
            else:
                return self.async_create_entry(
                    title="Person Tracker",
                    data={
                        CONF_CAMERAS: self._cameras,
                        CONF_AI_TASK_ENTITY: ai_task_entity,
                    },
                )
        elif self._reconfiguring:
            user_input = dict(self._get_reconfigure_entry().data)
        else:
            user_input = {CONF_AI_TASK_ENTITY: get_default_ai_task_entity_id(self.hass)}
        return self.async_show_form(
            step_id="ai_task",
            data_schema=self.add_suggested_values_to_schema(
                _AI_TASK_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which global setting to change."""
        self._cameras = list(self._get_reconfigure_entry().data[CONF_CAMERAS])
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["camera_add", "camera_edit", "camera_remove", "ai_task"],
        )

    async def async_step_camera_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the camera whose triggers to change."""
        if user_input is None:
            return self.async_show_form(
                step_id="camera_edit",
                data_schema=_configured_cameras_schema(CONF_CAMERA, self._camera_ids()),
            )
        self._edit_camera = user_input[CONF_CAMERA]
        return await self.async_step_camera_triggers()

    async def async_step_camera_triggers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the triggers of the picked camera."""
        index = self._camera_ids().index(self._edit_camera)
        errors: dict[str, str] = {}
        if user_input is not None:
            triggers = user_input.get(CONF_TRIGGERS) or []
            if error := triggers_error(self.hass, triggers):
                errors[CONF_TRIGGERS] = error
            else:
                self._cameras[index] = {
                    CONF_CAMERA: self._edit_camera,
                    CONF_TRIGGERS: triggers,
                }
                return self._save(**{CONF_CAMERAS: self._cameras})
        return self.async_show_form(
            step_id="camera_triggers",
            data_schema=self.add_suggested_values_to_schema(
                _TRIGGERS_SCHEMA, user_input or self._cameras[index]
            ),
            description_placeholders={"camera": self._edit_camera},
            errors=errors,
        )

    async def async_step_camera_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove cameras from the map."""
        errors: dict[str, str] = {}
        if user_input is not None:
            removed = set(user_input[CONF_REMOVE_CAMERAS])
            cameras = [c for c in self._cameras if c[CONF_CAMERA] not in removed]
            if not cameras:
                errors[CONF_REMOVE_CAMERAS] = ERR_NO_CAMERAS
            else:
                return self._save(**{CONF_CAMERAS: cameras})
        return self.async_show_form(
            step_id="camera_remove",
            data_schema=_configured_cameras_schema(
                CONF_REMOVE_CAMERAS, self._camera_ids()
            ),
            errors=errors,
        )


class PersonSubentryFlow(ConfigSubentryFlow):
    """Add or edit one person to track."""

    def _taken_slugs(self, exclude: str | None = None) -> set[str]:
        """Slugs of the other people of the entry."""
        return {
            person_slug(subentry.data[CONF_PERSON_NAME])
            for subentry in self._get_entry().get_subentries_of_type(SUBENTRY_PERSON)
            if subentry.subentry_id != exclude
        }

    def _validate(
        self, user_input: dict[str, Any], exclude: str | None = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Validate *user_input* and build the person's subentry data.

        Returns (data, errors); *data* is empty when *errors* is set.
        """
        name = user_input[CONF_PERSON_NAME]
        authoritative = user_input.get(CONF_AUTHORITATIVE_TRACKER)
        errors: dict[str, str] = {}
        if error := person_name_error(name, self._taken_slugs(exclude)):
            errors[CONF_PERSON_NAME] = error
        elif authoritative and (
            error := authoritative_tracker_error(self.hass, authoritative)
        ):
            errors[CONF_AUTHORITATIVE_TRACKER] = error
        if errors:
            return {}, errors
        data: dict[str, Any] = {
            CONF_PERSON_NAME: name,
            CONF_TTL: int(user_input.get(CONF_TTL, DEFAULT_TTL_MINUTES)),
        }
        if authoritative:
            data[CONF_AUTHORITATIVE_TRACKER] = authoritative
        return data, {}

    def _show_form(
        self,
        step_id: str,
        values: dict[str, Any] | None,
        errors: dict[str, str] | None = None,
    ) -> SubentryFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(_PERSON_SCHEMA, values),
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a person."""
        if user_input is None:
            return self._show_form("user", None)
        data, errors = self._validate(user_input)
        if errors:
            return self._show_form("user", user_input, errors)
        return self.async_create_entry(
            title=data[CONF_PERSON_NAME],
            data=data,
            unique_id=person_slug(data[CONF_PERSON_NAME]),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a person."""
        subentry = self._get_reconfigure_subentry()
        if user_input is None:
            return self._show_form("reconfigure", dict(subentry.data))
        data, errors = self._validate(user_input, exclude=subentry.subentry_id)
        if errors:
            return self._show_form("reconfigure", user_input, errors)
        # The entry's update listener reloads it.
        return self.async_update_and_abort(
            self._get_entry(),
            subentry,
            title=data[CONF_PERSON_NAME],
            data=data,
            unique_id=person_slug(data[CONF_PERSON_NAME]),
        )
