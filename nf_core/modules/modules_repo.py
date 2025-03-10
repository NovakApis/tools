import filecmp
import logging
import os
import shutil
from pathlib import Path

import git
import rich
import rich.progress
from git.exc import GitCommandError, InvalidGitRepositoryError

import nf_core.modules.modules_json
import nf_core.modules.modules_utils
from nf_core.utils import NFCORE_DIR, load_tools_config

log = logging.getLogger(__name__)

# Constants for the nf-core/modules repo used throughout the module files
NF_CORE_MODULES_NAME = "nf-core"
NF_CORE_MODULES_REMOTE = "https://github.com/nf-core/modules.git"
NF_CORE_MODULES_DEFAULT_BRANCH = "master"


class RemoteProgressbar(git.RemoteProgress):
    """
    An object to create a progressbar for when doing an operation with the remote.
    Note that an initialized rich Progress (progress bar) object must be past
    during initialization.
    """

    def __init__(self, progress_bar, repo_name, remote_url, operation):
        """
        Initializes the object and adds a task to the progressbar passed as 'progress_bar'

        Args:
            progress_bar (rich.progress.Progress): A rich progress bar object
            repo_name (str): Name of the repository the operation is performed on
            remote_url (str): Git URL of the repository the operation is performed on
            operation (str): The operation performed on the repository, i.e. 'Pulling', 'Cloning' etc.
        """
        super().__init__()
        self.progress_bar = progress_bar
        self.tid = self.progress_bar.add_task(
            f"{operation} from [bold green]'{repo_name}'[/bold green] ([link={remote_url}]{remote_url}[/link])",
            start=False,
            state="Waiting for response",
        )

    def update(self, op_code, cur_count, max_count=None, message=""):
        """
        Overrides git.RemoteProgress.update.
        Called every time there is a change in the remote operation
        """
        if not self.progress_bar.tasks[self.tid].started:
            self.progress_bar.start_task(self.tid)
        self.progress_bar.update(
            self.tid, total=max_count, completed=cur_count, state=f"{cur_count / max_count * 100:.1f}%"
        )


class ModulesRepo:
    """
    An object to store details about the repository being used for modules.

    Used by the `nf-core modules` top-level command with -r and -b flags,
    so that this can be used in the same way by all sub-commands.

    We keep track of the pull-status of the different installed repos in
    the static variable local_repo_status. This is so we don't need to
    pull a remote several times in one command.
    """

    local_repo_statuses = {}
    no_pull_global = False

    @staticmethod
    def local_repo_synced(repo_name):
        """
        Checks whether a local repo has been cloned/pull in the current session
        """
        return ModulesRepo.local_repo_statuses.get(repo_name, False)

    @staticmethod
    def update_local_repo_status(repo_name, up_to_date):
        """
        Updates the clone/pull status of a local repo
        """
        ModulesRepo.local_repo_statuses[repo_name] = up_to_date

    @staticmethod
    def get_remote_branches(remote_url):
        """
        Get all branches from a remote repository

        Args:
            remote_url (str): The git url to the remote repository

        Returns:
            (set[str]): All branches found in the remote
        """
        try:
            unparsed_branches = git.Git().ls_remote(remote_url)
        except git.GitCommandError:
            raise LookupError(f"Was unable to fetch branches from '{remote_url}'")
        else:
            branches = {}
            for branch_info in unparsed_branches.split("\n"):
                sha, name = branch_info.split("\t")
                if name != "HEAD":
                    # The remote branches are shown as 'ref/head/branch'
                    branch_name = Path(name).stem
                    branches[sha] = branch_name
            return set(branches.values())

    def __init__(self, remote_url=None, branch=None, no_pull=False, hide_progress=False):
        """
        Initializes the object and clones the git repository if it is not already present
        """

        # This allows us to set this one time and then keep track of the user's choice
        ModulesRepo.no_pull_global |= no_pull

        # Check if the remote seems to be well formed
        if remote_url is None:
            remote_url = NF_CORE_MODULES_REMOTE

        self.remote_url = remote_url

        self.fullname = nf_core.modules.modules_utils.repo_full_name_from_remote(self.remote_url)

        self.setup_local_repo(remote_url, branch, hide_progress)

        config_fn, repo_config = load_tools_config(self.local_repo_dir)
        try:
            self.repo_path = repo_config["org_path"]
        except KeyError:
            raise UserWarning(f"'org_path' key not present in {config_fn.name}")

        # Verify that the repo seems to be correctly configured
        if self.repo_path != NF_CORE_MODULES_NAME or self.branch:
            self.verify_branch()

        # Convenience variable
        self.modules_dir = os.path.join(self.local_repo_dir, "modules", self.repo_path)
        self.subworkflows_dir = os.path.join(self.local_repo_dir, "subworkflows", self.repo_path)

        self.avail_module_names = None

    def verify_sha(self, prompt, sha):
        """
        Verify that 'sha' and 'prompt' arguments are not provided together.
        Verify that the provided SHA exists in the repo.

        Arguments:
            prompt (bool):              prompt asking for SHA
            sha (str):                  provided sha
        """
        if prompt and sha is not None:
            log.error("Cannot use '--sha' and '--prompt' at the same time!")
            return False

        if sha:
            if not self.sha_exists_on_branch(sha):
                log.error(f"Commit SHA '{sha}' doesn't exist in '{self.remote_url}'")
                return False

        return True

    def setup_local_repo(self, remote, branch, hide_progress=True):
        """
        Sets up the local git repository. If the repository has been cloned previously, it
        returns a git.Repo object of that clone. Otherwise it tries to clone the repository from
        the provided remote URL and returns a git.Repo of the new clone.

        Args:
            remote (str): git url of remote
            branch (str): name of branch to use
        Sets self.repo
        """
        self.local_repo_dir = os.path.join(NFCORE_DIR, self.fullname)
        try:
            if not os.path.exists(self.local_repo_dir):
                try:
                    pbar = rich.progress.Progress(
                        "[bold blue]{task.description}",
                        rich.progress.BarColumn(bar_width=None),
                        "[bold yellow]{task.fields[state]}",
                        transient=True,
                        disable=hide_progress or os.environ.get("HIDE_PROGRESS", None) is not None,
                    )
                    with pbar:
                        self.repo = git.Repo.clone_from(
                            remote,
                            self.local_repo_dir,
                            progress=RemoteProgressbar(pbar, self.fullname, self.remote_url, "Cloning"),
                        )
                    ModulesRepo.update_local_repo_status(self.fullname, True)
                except GitCommandError:
                    raise LookupError(f"Failed to clone from the remote: `{remote}`")
                # Verify that the requested branch exists by checking it out
                self.setup_branch(branch)
            else:
                self.repo = git.Repo(self.local_repo_dir)

                if ModulesRepo.no_pull_global:
                    ModulesRepo.update_local_repo_status(self.fullname, True)
                # If the repo is already cloned, fetch the latest changes from the remote
                if not ModulesRepo.local_repo_synced(self.fullname):
                    pbar = rich.progress.Progress(
                        "[bold blue]{task.description}",
                        rich.progress.BarColumn(bar_width=None),
                        "[bold yellow]{task.fields[state]}",
                        transient=True,
                        disable=hide_progress or os.environ.get("HIDE_PROGRESS", None) is not None,
                    )
                    with pbar:
                        self.repo.remotes.origin.fetch(
                            progress=RemoteProgressbar(pbar, self.fullname, self.remote_url, "Pulling")
                        )
                    ModulesRepo.update_local_repo_status(self.fullname, True)

                # Before verifying the branch, fetch the changes
                # Verify that the requested branch exists by checking it out
                self.setup_branch(branch)

                # Now merge the changes
                tracking_branch = self.repo.active_branch.tracking_branch()
                if tracking_branch is None:
                    raise LookupError(f"There is no remote tracking branch '{self.branch}' in '{self.remote_url}'")
                self.repo.git.merge(tracking_branch.name)
        except (GitCommandError, InvalidGitRepositoryError) as e:
            log.error(f"[red]Could not set up local cache of modules repository:[/]\n{e}\n")
            if rich.prompt.Confirm.ask(f"[violet]Delete local cache '{self.local_repo_dir}' and try again?"):
                log.info(f"Removing '{self.local_repo_dir}'")
                shutil.rmtree(self.local_repo_dir)
                self.setup_local_repo(remote, branch, hide_progress)
            else:
                raise LookupError("Exiting due to error with local modules git repo")

    def setup_branch(self, branch):
        """
        Verify that we have a branch and otherwise use the default one.
        The branch is then checked out to verify that it exists in the repo.

        Args:
            branch (str): Name of branch
        """
        if branch is None:
            # Don't bother fetching default branch if we're using nf-core
            if self.remote_url == NF_CORE_MODULES_REMOTE:
                self.branch = "master"
            else:
                self.branch = self.get_default_branch()
        else:
            self.branch = branch

        # Verify that the branch exists by checking it out
        self.branch_exists()

    def get_default_branch(self):
        """
        Gets the default branch for the repo (the branch origin/HEAD is pointing to)
        """
        origin_head = next(ref for ref in self.repo.refs if ref.name == "origin/HEAD")
        _, branch = origin_head.ref.name.split("/")
        return branch

    def branch_exists(self):
        """
        Verifies that the branch exists in the repository by trying to check it out
        """
        try:
            self.checkout_branch()
        except GitCommandError:
            raise LookupError(f"Branch '{self.branch}' not found in '{self.remote_url}'")

    def verify_branch(self):
        """
        Verifies the active branch conforms do the correct directory structure
        """
        dir_names = os.listdir(self.local_repo_dir)
        if "modules" not in dir_names:
            err_str = f"Repository '{self.remote_url}' ({self.branch}) does not contain the 'modules/' directory"
            if "software" in dir_names:
                err_str += (
                    ".\nAs of nf-core/tools version 2.0, the 'software/' directory should be renamed to 'modules/'"
                )
            raise LookupError(err_str)

    def checkout_branch(self):
        """
        Checks out the specified branch of the repository
        """
        self.repo.git.checkout(self.branch)

    def checkout(self, commit):
        """
        Checks out the repository at the requested commit

        Args:
            commit (str): Git SHA of the commit
        """
        self.repo.git.checkout(commit)

    def component_exists(self, component_name, component_type, checkout=True):
        """
        Check if a module/subworkflow exists in the branch of the repo

        Args:
            component_name (str): The name of the module/subworkflow

        Returns:
            (bool): Whether the module/subworkflow exists in this branch of the repository
        """
        return component_name in self.get_avail_components(component_type, checkout=checkout)

    def get_component_dir(self, component_name, component_type):
        """
        Returns the file path of a module/subworkflow directory in the repo.
        Does not verify that the path exists.
        Args:
            component_name (str): The name of the module/subworkflow

        Returns:
            component_path (str): The path of the module/subworkflow in the local copy of the repository
        """
        if component_type == "modules":
            return os.path.join(self.modules_dir, component_name)
        elif component_type == "subworkflows":
            return os.path.join(self.subworkflows_dir, component_name)

    def install_component(self, component_name, install_dir, commit, component_type):
        """
        Install the module/subworkflow files into a pipeline at the given commit

        Args:
            component_name (str): The name of the module/subworkflow
            install_dir (str): The path where the module/subworkflow should be installed
            commit (str): The git SHA for the version of the module/subworkflow to be installed

        Returns:
            (bool): Whether the operation was successful or not
        """
        # Check out the repository at the requested ref
        try:
            self.checkout(commit)
        except git.GitCommandError:
            return False

        # Check if the module/subworkflow exists in the branch
        if not self.component_exists(component_name, component_type, checkout=False):
            log.error(
                f"The requested {component_type[:-1]} does not exists in the branch '{self.branch}' of {self.remote_url}'"
            )
            return False

        # Copy the files from the repo to the install folder
        shutil.copytree(self.get_component_dir(component_name, component_type), Path(install_dir, component_name))

        # Switch back to the tip of the branch
        self.checkout_branch()
        return True

    def module_files_identical(self, module_name, base_path, commit):
        """
        Checks whether the module files in a pipeline are identical to the ones in the remote
        Args:
            module_name (str): The name of the module
            base_path (str): The path to the module in the pipeline

        Returns:
            (bool): Whether the pipeline files are identical to the repo files
        """
        if commit is None:
            self.checkout_branch()
        else:
            self.checkout(commit)
        module_files = ["main.nf", "meta.yml"]
        files_identical = {file: True for file in module_files}
        module_dir = self.get_component_dir(module_name, "modules")
        for file in module_files:
            try:
                files_identical[file] = filecmp.cmp(os.path.join(module_dir, file), os.path.join(base_path, file))
            except FileNotFoundError:
                log.debug(f"Could not open file: {os.path.join(module_dir, file)}")
                continue
        self.checkout_branch()
        return files_identical

    def get_component_git_log(self, component_name, component_type, depth=None):
        """
        Fetches the commit history the of requested module/subworkflow since a given date. The default value is
        not arbitrary - it is the last time the structure of the nf-core/modules repository was had an
        update breaking backwards compatibility.
        Args:
            component_name (str): Name of module/subworkflow
            modules_repo (ModulesRepo): A ModulesRepo object configured for the repository in question

        Returns:
            ( dict ): Iterator of commit SHAs and associated (truncated) message
        """
        self.checkout_branch()
        component_path = os.path.join(component_type, self.repo_path, component_name)
        commits_new = self.repo.iter_commits(max_count=depth, paths=component_path)
        commits_new = [
            {"git_sha": commit.hexsha, "trunc_message": commit.message.partition("\n")[0]} for commit in commits_new
        ]
        commits_old = []
        if component_type == "modules":
            # Grab commits also from previous modules structure
            component_path = os.path.join("modules", component_name)
            commits_old = self.repo.iter_commits(max_count=depth, paths=component_path)
            commits_old = [
                {"git_sha": commit.hexsha, "trunc_message": commit.message.partition("\n")[0]} for commit in commits_old
            ]
        commits = iter(commits_new + commits_old)
        return commits

    def get_latest_component_version(self, component_name, component_type):
        """
        Returns the latest commit in the repository
        """
        return list(self.get_component_git_log(component_name, component_type, depth=1))[0]["git_sha"]

    def sha_exists_on_branch(self, sha):
        """
        Verifies that a given commit sha exists on the branch
        """
        self.checkout_branch()
        return sha in (commit.hexsha for commit in self.repo.iter_commits())

    def get_commit_info(self, sha):
        """
        Fetches metadata about the commit (dates, message, etc.)
        Args:
            commit_sha (str): The SHA of the requested commit
        Returns:
            message (str): The commit message for the requested commit
            date (str): The commit date for the requested commit
        Raises:
            LookupError: If the search for the commit fails
        """
        self.checkout_branch()
        for commit in self.repo.iter_commits():
            if commit.hexsha == sha:
                message = commit.message.partition("\n")[0]
                date_obj = commit.committed_datetime
                date = str(date_obj.date())
                return message, date
        raise LookupError(f"Commit '{sha}' not found in the '{self.remote_url}'")

    def get_avail_components(self, component_type, checkout=True):
        """
        Gets the names of the modules/subworkflows in the repository. They are detected by
        checking which directories have a 'main.nf' file

        Returns:
            ([ str ]): The module/subworkflow names
        """
        if checkout:
            self.checkout_branch()
        # Get directory
        if component_type == "modules":
            directory = self.modules_dir
        elif component_type == "subworkflows":
            directory = self.subworkflows_dir
        # Module/Subworkflow directories are characterized by having a 'main.nf' file
        avail_component_names = [
            os.path.relpath(dirpath, start=directory)
            for dirpath, _, file_names in os.walk(directory)
            if "main.nf" in file_names
        ]
        return avail_component_names

    def get_meta_yml(self, component_type, module_name):
        """
        Returns the contents of the 'meta.yml' file of a module

        Args:
            module_name (str): The name of the module

        Returns:
            (str): The contents of the file in text format
        """
        self.checkout_branch()
        if component_type == "modules":
            path = Path(self.modules_dir, module_name, "meta.yml")
        elif component_type == "subworkflows":
            path = Path(self.subworkflows_dir, module_name, "meta.yml")
        else:
            raise ValueError(f"Invalid component type: {component_type}")
        if not path.exists():
            return None
        with open(path) as fh:
            contents = fh.read()
        return contents
