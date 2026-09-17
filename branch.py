# Copyright (c) 2024 PJSC VimpelCom
from typing import Optional

DEFAULT_BRANCH = "main"


def normalize_branch_name(branch: Optional[str]) -> str:
    if branch is None:
        return DEFAULT_BRANCH
    if not branch.strip():
        raise ValueError("Параметр branch не может быть пустым")
    return branch.strip().lower()
