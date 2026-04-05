"""Backward-compatibility re-export — canonical module is agp.plugins._via_file."""

from agp.plugins._via_file import (  # noqa: F401
    build_task_file_content,
    cleanup_stale_task_files,
    cleanup_task_file,
    reference_string,
    write_task_file,
)
