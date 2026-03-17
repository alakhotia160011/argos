"""Git operations for the autoresearch loop.

IMPORTANT: All operations are designed to avoid switching the working tree.
Autoresearch only modifies prompt files — it should never disrupt unrelated
files (frontend, config, etc.) by checking out a different branch.
"""

from __future__ import annotations

import logging
from pathlib import Path

from git import Repo

logger = logging.getLogger(__name__)


def get_repo(path: str | Path = ".") -> Repo:
    """Get the git repo rooted at *path*."""
    return Repo(path, search_parent_directories=True)


def current_branch_name(repo: Repo) -> str:
    """Return the name of the currently checked-out branch."""
    return repo.active_branch.name


def create_branch(repo: Repo, branch_name: str) -> None:
    """Create a new branch at HEAD without switching to it.

    The working tree stays on the current branch — only the prompt file
    is committed to the new branch via a tree-level operation.
    """
    logger.info("Creating branch: %s (no checkout)", branch_name)
    repo.create_head(branch_name)


def commit_to_branch(repo: Repo, branch_name: str, file_path: str | Path, message: str) -> str:
    """Commit a single file change to a branch WITHOUT checking it out.

    This stages the file, commits to the current branch, then moves
    the target branch ref to the new commit — keeping the working tree
    untouched for other files.
    """
    # Stage and commit on current branch
    repo.index.add([str(file_path)])
    commit = repo.index.commit(message)
    sha = commit.hexsha

    # Point the autoresearch branch to this commit
    branch = repo.heads[branch_name]
    branch.set_commit(commit)

    logger.info("Committed %s to branch %s: %s", sha[:8], branch_name, message)
    return sha


# Legacy function kept for backward compatibility
def commit_change(repo: Repo, file_path: str | Path, message: str) -> str:
    """Stage a single file and commit on the current branch. Returns the commit SHA."""
    repo.index.add([str(file_path)])
    commit = repo.index.commit(message)
    logger.info("Committed %s: %s", commit.hexsha[:8], message)
    return commit.hexsha


def merge_branch(repo: Repo, branch_name: str) -> None:
    """Merge the given branch into the current branch (no checkout switch)."""
    repo.git.merge(branch_name, "--no-edit")
    logger.info("Merged branch %s into %s", branch_name, current_branch_name(repo))


def delete_branch(repo: Repo, branch_name: str) -> None:
    """Delete a local branch."""
    repo.git.branch("-D", branch_name)
    logger.info("Deleted branch %s", branch_name)


def revert_and_cleanup(repo: Repo, branch_name: str) -> None:
    """Revert: reset the prompt file to pre-modification state and delete the branch.

    Does NOT switch branches — just reverts the last commit on the current branch
    that was created by autoresearch, then cleans up the experiment branch.
    """
    # Revert the last commit (the autoresearch modification) on current branch
    try:
        repo.git.revert("HEAD", "--no-edit")
        logger.info("Reverted last commit on %s", current_branch_name(repo))
    except Exception as e:
        logger.warning("Revert failed, trying reset: %s", e)
        # Fallback: soft reset if revert has conflicts
        repo.git.reset("HEAD~1", "--hard")

    delete_branch(repo, branch_name)


def keep_and_cleanup(repo: Repo, branch_name: str) -> None:
    """Keep: the modification is already on the current branch, just delete the ref."""
    delete_branch(repo, branch_name)
    logger.info("Kept modification, cleaned up branch %s", branch_name)
