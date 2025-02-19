# ***************************************************************************
# *                                                                         *
# *   Copyright (c) 2021 Chris Hennes <chennes@pioneerlibrarysystem.org>    *
# *                                                                         *
# *   This program is free software; you can redistribute it and/or modify  *
# *   it under the terms of the GNU Lesser General Public License (LGPL)    *
# *   as published by the Free Software Foundation; either version 2 of     *
# *   the License, or (at your option) any later version.                   *
# *   for detail see the LICENCE text file.                                 *
# *                                                                         *
# *   This program is distributed in the hope that it will be useful,       *
# *   but WITHOUT ANY WARRANTY; without even the implied warranty of        *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         *
# *   GNU Library General Public License for more details.                  *
# *                                                                         *
# *   You should have received a copy of the GNU Library General Public     *
# *   License along with this program; if not, write to the Free Software   *
# *   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  *
# *   USA                                                                   *
# *                                                                         *
# ***************************************************************************

import FreeCAD

import os
from urllib.parse import urlparse
from typing import Dict, Set
from threading import Lock

from addonmanager_macro import Macro
import addonmanager_utilities as utils

translate = FreeCAD.Qt.translate


class AddonManagerRepo:
    "Encapsulate information about a FreeCAD addon"

    from enum import IntEnum

    class RepoType(IntEnum):
        WORKBENCH = 1
        MACRO = 2
        PACKAGE = 3

        def __str__(self) -> str:
            if self.value == 1:
                return "Workbench"
            elif self.value == 2:
                return "Macro"
            elif self.value == 3:
                return "Package"

    class UpdateStatus(IntEnum):
        NOT_INSTALLED = 0
        UNCHECKED = 1
        NO_UPDATE_AVAILABLE = 2
        UPDATE_AVAILABLE = 3
        PENDING_RESTART = 4
        CANNOT_CHECK = 5  # If we don't have git, etc.

        def __lt__(self, other):
            if self.__class__ is other.__class__:
                return self.value < other.value
            return NotImplemented

        def __str__(self) -> str:
            if self.value == 0:
                return "Not installed"
            elif self.value == 1:
                return "Unchecked"
            elif self.value == 2:
                return "No update available"
            elif self.value == 3:
                return "Update available"
            elif self.value == 4:
                return "Restart required"
            elif self.value == 5:
                return "Can't check"

    class Dependencies:
        def __init__(self):
            self.required_external_addons = dict()
            self.blockers = dict()
            self.replaces = dict()
            self.unrecognized_addons: Set[str] = set()
            self.python_required: Set[str] = set()
            self.python_optional: Set[str] = set()

    class ResolutionFailed(RuntimeError):
        def __init__(self, msg):
            super().__init__(msg)

    def __init__(self, name: str, url: str, status: UpdateStatus, branch: str):
        self.name = name.strip()
        self.display_name = self.name
        self.url = url.strip()
        self.branch = branch.strip()
        self.python2 = False
        self.obsolete = False
        self.rejected = False
        self.repo_type = AddonManagerRepo.RepoType.WORKBENCH
        self.description = None
        self.tags = set()  # Just a cache, loaded from Metadata

        # To prevent multiple threads from running git actions on this repo at the same time
        self.git_lock = Lock()

        # To prevent multiple threads from accessing the status at the same time
        self.status_lock = Lock()
        self.set_status(status)

        from addonmanager_utilities import construct_git_url

        # The url should never end in ".git", so strip it if it's there
        parsed_url = urlparse(self.url)
        if parsed_url.path.endswith(".git"):
            self.url = parsed_url.scheme + parsed_url.path[:-4]
            if parsed_url.query:
                self.url += "?" + parsed_url.query
            if parsed_url.fragment:
                self.url += "#" + parsed_url.fragment

        if utils.recognized_git_location(self):
            self.metadata_url = construct_git_url(self, "package.xml")
        else:
            self.metadata_url = None
        self.metadata = None
        self.icon = None
        self.cached_icon_filename = ""
        self.macro = None  # Bridge to Gaël Écorchard's macro management class
        self.updated_timestamp = None
        self.installed_version = None

        # Each repo is also a node in a directed dependency graph (referenced by name so
        # they cen be serialized):
        self.requires: Set[str] = set()
        self.blocks: Set[str] = set()

        # And maintains a list of required and optional Python dependencies from metadata.txt
        self.python_requires: Set[str] = set()
        self.python_optional: Set[str] = set()

    def __str__(self) -> str:
        result = f"FreeCAD {self.repo_type}\n"
        result += f"Name: {self.name}\n"
        result += f"URL: {self.url}\n"
        result += (
            "Has metadata\n" if self.metadata is not None else "No metadata found\n"
        )
        if self.macro is not None:
            result += "Has linked Macro object\n"
        return result

    @classmethod
    def from_macro(self, macro: Macro):
        if macro.is_installed():
            status = AddonManagerRepo.UpdateStatus.UNCHECKED
        else:
            status = AddonManagerRepo.UpdateStatus.NOT_INSTALLED
        instance = AddonManagerRepo(macro.name, macro.url, status, "master")
        instance.macro = macro
        instance.repo_type = AddonManagerRepo.RepoType.MACRO
        instance.description = macro.desc
        return instance

    @classmethod
    def from_cache(self, cache_dict: Dict):
        """Load basic data from cached dict data. Does not include Macro or Metadata information, which must be populated separately."""

        mod_dir = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", cache_dict["name"])
        if os.path.isdir(mod_dir):
            status = AddonManagerRepo.UpdateStatus.UNCHECKED
        else:
            status = AddonManagerRepo.UpdateStatus.NOT_INSTALLED
        instance = AddonManagerRepo(
            cache_dict["name"], cache_dict["url"], status, cache_dict["branch"]
        )

        for key, value in cache_dict.items():
            instance.__dict__[key] = value

        instance.repo_type = AddonManagerRepo.RepoType(cache_dict["repo_type"])
        if instance.repo_type == AddonManagerRepo.RepoType.PACKAGE:
            # There must be a cached metadata file, too
            cached_package_xml_file = os.path.join(
                FreeCAD.getUserCachePath(),
                "AddonManager",
                "PackageMetadata",
                instance.name,
            )
            if os.path.isfile(cached_package_xml_file):
                instance.load_metadata_file(cached_package_xml_file)

        if "requires" in cache_dict:
            instance.requires = set(cache_dict["requires"])
            instance.blocks = set(cache_dict["blocks"])
            instance.python_requires = set(cache_dict["python_requires"])
            instance.python_optional = set(cache_dict["python_optional"])

        return instance

    def to_cache(self) -> Dict:
        """Returns a dictionary with cache information that can be used later with from_cache to recreate this object."""

        return {
            "name": self.name,
            "display_name": self.display_name,
            "url": self.url,
            "branch": self.branch,
            "repo_type": int(self.repo_type),
            "description": self.description,
            "cached_icon_filename": self.get_cached_icon_filename(),
            "python2": self.python2,
            "obsolete": self.obsolete,
            "rejected": self.rejected,
            "requires": list(self.requires),
            "blocks": list(self.blocks),
            "python_requires": list(self.python_requires),
            "python_optional": list(self.python_optional),
        }

    def load_metadata_file(self, file: str) -> None:
        if os.path.exists(file):
            metadata = FreeCAD.Metadata(file)
            self.set_metadata(metadata)
        else:
            FreeCAD.Console.PrintMessage(
                "Internal error: {} does not exist".format(file)
            )

    def set_metadata(self, metadata: FreeCAD.Metadata) -> None:
        self.metadata = metadata
        self.display_name = metadata.Name
        self.repo_type = AddonManagerRepo.RepoType.PACKAGE
        self.description = metadata.Description
        for url in metadata.Urls:
            if "type" in url and url["type"] == "repository":
                self.url = url["location"]
                if "branch" in url:
                    self.branch = url["branch"]
                else:
                    self.branch = "master"
        self.extract_tags(self.metadata)

    def extract_tags(self, metadata: FreeCAD.Metadata) -> None:
        for new_tag in metadata.Tag:
            self.tags.add(new_tag)

        content = metadata.Content
        for key, value in content.items():
            for item in value:
                self.extract_tags(item)

    def contains_workbench(self) -> bool:
        """Determine if this package contains (or is) a workbench"""

        if self.repo_type == AddonManagerRepo.RepoType.WORKBENCH:
            return True
        elif self.repo_type == AddonManagerRepo.RepoType.PACKAGE:
            if self.metadata is None:
                FreeCAD.Console.PrintWarning(
                    f"Addon Manager internal error: lost metadata for package {self.name}\n"
                )
                return False
            content = self.metadata.Content
            if not content:
                FreeCAD.Console.PrintLog(
                    f"Package {self.display_name} does not list any content items in its package.xml metadata file.\n"
                )
                return False
            return "workbench" in content
        else:
            return False

    def contains_macro(self) -> bool:
        """Determine if this package contains (or is) a macro"""

        if self.repo_type == AddonManagerRepo.RepoType.MACRO:
            return True
        elif self.repo_type == AddonManagerRepo.RepoType.PACKAGE:
            if self.metadata is None:
                FreeCAD.Console.PrintWarning(
                    f"Addon Manager internal error: lost metadata for package {self.name}\n"
                )
                return False
            content = self.metadata.Content
            return "macro" in content
        else:
            return False

    def contains_preference_pack(self) -> bool:
        """Determine if this package contains a preference pack"""

        if self.repo_type == AddonManagerRepo.RepoType.PACKAGE:
            if self.metadata is None:
                FreeCAD.Console.PrintWarning(
                    f"Addon Manager internal error: lost metadata for package {self.name}\n"
                )
                return False
            content = self.metadata.Content
            return "preferencepack" in content
        else:
            return False

    def get_cached_icon_filename(self) -> str:
        """Get the filename for the locally-cached copy of the icon"""

        if self.cached_icon_filename:
            return self.cached_icon_filename

        if not self.metadata:
            return ""

        real_icon = self.metadata.Icon
        if not real_icon:
            # If there is no icon set for the entire package, see if there are any workbenches, which
            # are required to have icons, and grab the first one we find:
            content = self.metadata.Content
            if "workbench" in content:
                wb = content["workbench"][0]
                if wb.Icon:
                    if wb.Subdirectory:
                        subdir = wb.Subdirectory
                    else:
                        subdir = wb.Name
                    real_icon = subdir + wb.Icon

        real_icon = real_icon.replace(
            "/", os.path.sep
        )  # Required path separator in the metadata.xml file to local separator

        _, file_extension = os.path.splitext(real_icon)
        store = os.path.join(
            FreeCAD.getUserCachePath(), "AddonManager", "PackageMetadata"
        )
        self.cached_icon_filename = os.path.join(
            store, self.name, "cached_icon" + file_extension
        )

        return self.cached_icon_filename

    def walk_dependency_tree(self, all_repos, deps):
        """Compute the total dependency tree for this repo (recursive)"""

        deps.python_required |= self.python_requires
        deps.python_optional |= self.python_optional
        for dep in self.requires:
            if dep in all_repos:
                if not dep in deps.required:
                    deps.required_external_addons.append(all_repos[dep])
                    all_repos[dep].walk_dependency_tree(all_repos, deps)
            else:
                # Maybe this is an internal workbench, just store its name
                deps.unrecognized_addons.add(dep)
        for dep in self.blocks:
            if dep in all_repos:
                deps.blockers[dep] = all_repos[dep]

    def status(self):
        with self.status_lock:
            return self.update_status

    def set_status(self, status):
        with self.status_lock:
            self.update_status = status

    def is_disabled(self):
        # Check for existence of disabling stopfile:
        stopfile = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", self.name, "ADDON_DISABLED")
        if os.path.exists(stopfile):
            return True
        else:
            return False

    def disable(self):
        stopfile = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", self.name, "ADDON_DISABLED")
        with open(stopfile,"w") as f:
            f.write("The existence of this file prevents FreeCAD from loading this Addon. To re-enable, delete the file.")

    def enable(self):
        stopfile = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", self.name, "ADDON_DISABLED")
        try:
            os.unlink(stopfile)
        except Exception:
            pass

