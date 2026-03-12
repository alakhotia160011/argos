"""Git operations for the autoresearch loop."""

from __future__ import annotations

import logging
from pathlib import Path

from git import Repo

logger = logging.getLogger(__name__)


def get_repo(path: str | Path = ".") -> Repo:
    """Get the git repo rooted at *path*."""
    return Repo(path, search_parent_directories=True)


def create_branch(repo: Repo, branch_name: str) -> None:
    """Create and checkout a new branch from current HEAD."""
    logger.info("Creating branch: %s", branch_name)
    repo.git.checkout("-b", branch_name)


def commit_change(repo: Repo, file_path: str | Path, message: str) -> str:
    """Stage a single file and commit. Returns the commit SHA."""
    repo.index.add([str(file_path)])
    commit = repo.index.commit(message)
    logger.info("Committed %s: %s", commit.hexsha[:8], message)
    return commit.hexsha


def merge_branch(repo: Repo, branch_name: str) -> None:
    """Checkout main and merge the given branch."""
    main = repo.heads["main"]
    main.checkout()
    repo.git.merge(branch_name)
    logger.info("Merged branch %s into main", branch_name)


def delete_branch(repo: Repo, branch_name: str) -> None:
    """Delete a local branch."""
    repo.git.branch("-D", branch_name)
    logger.info("Deleted branch %s", branch_name)


def revert_and_cleanup(repo: Repo, branch_name: str) -> None:
    """Checkout main and delete the experiment branch."""
    main = repo.heads["main"]
    main.checkout()
    delete_branch(repo, branch_name)


def keep_and_cleanup(repo: Repo, branch_name: str) -> None:
    """Merge the experiment branch into main and clean up."""
    merge_branch(repo, branch_name)
    delete_branch(repo, branch_name)
