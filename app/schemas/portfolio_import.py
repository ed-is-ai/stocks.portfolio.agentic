"""Safe, UI-facing projection of import contract metadata (Story 3.3).

:class:`ProviderOption` exposes only what the import form needs to render
provider/account-type selectors -- never a contract's ``field_mapping``,
``header_aliases``, or other internal parsing details.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ProviderOption(BaseModel):
    """One selectable provider and the account types it declares.

    ``account_type_ids`` is the union of every loaded contract's declared
    account types for this ``provider_id`` -- an empty tuple means this
    provider has no account-type distinction at all (the account-type
    select must reject any selection in that case).
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )

    provider_id: str
    provider_name: str
    account_type_ids: tuple[str, ...] = ()


__all__ = ["ProviderOption"]
