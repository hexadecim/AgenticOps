class SettingsError(Exception):
    code = "settings_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownSectionError(SettingsError):
    code = "unknown_section"

    def __init__(self, section: str) -> None:
        super().__init__(f"There is no settings section called '{section}'.")


class InvalidSettingsError(SettingsError):
    code = "invalid_settings"

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("Some fields need attention.")
        self.errors = errors


class VersionConflictError(SettingsError):
    code = "version_conflict"

    def __init__(self) -> None:
        super().__init__(
            "Someone else saved these settings while you were editing. Reload to see their changes."
        )


class ReaderNotAllowedError(SettingsError):
    code = "reader_not_allowed"

    def __init__(self, service: str, section: str) -> None:
        super().__init__(f"Service {service} may not read the {section} settings.")
