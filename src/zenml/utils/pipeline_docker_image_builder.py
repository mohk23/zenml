#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""Implementation of Docker image builds to run ZenML pipelines."""

import itertools
import os
import subprocess
import sys
from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    DefaultDict,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

import zenml
from zenml.config import DockerSettings
from zenml.config.docker_settings import PythonEnvironmentExportMethod
from zenml.constants import (
    ENV_ZENML_CONFIG_PATH,
    ENV_ZENML_ENABLE_REPO_INIT_WARNINGS,
    ENV_ZENML_REQUIRES_CODE_DOWNLOAD,
)
from zenml.enums import OperatingSystemType
from zenml.integrations.registry import integration_registry
from zenml.logger import get_logger
from zenml.utils import docker_utils, io_utils, source_utils

if TYPE_CHECKING:
    from zenml.code_repositories import BaseCodeRepository
    from zenml.container_registries import BaseContainerRegistry
    from zenml.image_builders import BuildContext
    from zenml.stack import Stack

logger = get_logger(__name__)

DOCKER_IMAGE_WORKDIR = "/app"
DOCKER_IMAGE_ZENML_CONFIG_DIR = ".zenconfig"
DOCKER_IMAGE_ZENML_CONFIG_PATH = (
    f"{DOCKER_IMAGE_WORKDIR}/{DOCKER_IMAGE_ZENML_CONFIG_DIR}"
)

DEFAULT_DOCKER_PARENT_IMAGE = (
    f"zenmldocker/zenml:{zenml.__version__}-"
    f"py{sys.version_info.major}.{sys.version_info.minor}"
)


class PipelineDockerImageBuilder:
    """Builds Docker images to run a ZenML pipeline."""

    def build_docker_image(
        self,
        docker_settings: "DockerSettings",
        tag: str,
        stack: "Stack",
        include_files: bool,
        download_files: bool,
        entrypoint: Optional[str] = None,
        extra_files: Optional[Dict[str, str]] = None,
        code_repository: Optional["BaseCodeRepository"] = None,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Builds (and optionally pushes) a Docker image to run a pipeline.

        Use the image name returned by this method whenever you need to uniquely
        reference the pushed image in order to pull or run it.

        Args:
            docker_settings: The settings for the image build.
            tag: The tag to use for the image.
            stack: The stack on which the pipeline will be deployed.
            include_files: Whether to include files in the build context.
            download_files: Whether to download files in the build context.
            entrypoint: Entrypoint to use for the final image. If left empty,
                no entrypoint will be included in the image.
            extra_files: Extra files to add to the build context. Keys are the
                path inside the build context, values are either the file
                content or a file path.
            code_repository: The code repository from which files will be
                downloaded.

        Returns:
            A tuple (image_digest, dockerfile, requirements):
            - The Docker image repo digest or local name, depending on whether
            the image was pushed or is just stored locally.
            - Dockerfile will contain the contents of the Dockerfile used to
            build the image.
            - Requirements is a string with a single pip requirement per line.

        Raises:
            RuntimeError: If the stack does not contain an image builder.
            ValueError: If no Dockerfile and/or custom parent image is
                specified and the Docker configuration doesn't require an
                image build.
        """
        requirements: Optional[str] = None
        dockerfile: Optional[str] = None

        if docker_settings.skip_build:
            assert (
                docker_settings.parent_image
            )  # checked via validator already

            # Should we tag this here and push it to the container registry of
            # the stack to make sure it's always accessible when running the
            # pipeline?
            return docker_settings.parent_image, dockerfile, requirements

        stack.validate()
        image_builder = stack.image_builder
        if not image_builder:
            raise RuntimeError(
                "Unable to build Docker images without an image builder in the "
                f"stack `{stack.name}`."
            )

        container_registry = stack.container_registry

        build_context_class = image_builder.build_context_class
        target_image_name = self._get_target_image_name(
            docker_settings=docker_settings,
            tag=tag,
            container_registry=container_registry,
        )

        requires_zenml_build = any(
            [
                docker_settings.requirements,
                docker_settings.required_integrations,
                docker_settings.required_hub_plugins,
                docker_settings.replicate_local_python_environment,
                docker_settings.install_stack_requirements,
                docker_settings.apt_packages,
                docker_settings.environment,
                include_files,
                download_files,
                entrypoint,
                extra_files,
            ]
        )

        # Fallback to the value defined on the stack component if the
        # pipeline configuration doesn't have a configured value
        parent_image = (
            docker_settings.parent_image or DEFAULT_DOCKER_PARENT_IMAGE
        )

        if docker_settings.dockerfile:
            if parent_image != DEFAULT_DOCKER_PARENT_IMAGE:
                logger.warning(
                    "You've specified both a Dockerfile and a custom parent "
                    "image, ignoring the parent image."
                )

            push = (
                not image_builder.is_building_locally
                or not requires_zenml_build
            )

            if requires_zenml_build:
                # We will build an additional image on top of this one later
                # to include user files and/or install requirements. The image
                # we build now will be used as the parent for the next build.
                user_image_name = (
                    f"{docker_settings.target_repository}:"
                    f"{tag}-intermediate-build"
                )
                if push and container_registry:
                    user_image_name = (
                        f"{container_registry.config.uri}/{user_image_name}"
                    )

                parent_image = user_image_name
            else:
                # The image we'll build from the custom Dockerfile will be
                # used directly, so we tag it with the requested target name.
                user_image_name = target_image_name

            build_context = build_context_class(
                root=docker_settings.build_context_root
            )
            build_context.add_file(
                source=docker_settings.dockerfile, destination="Dockerfile"
            )
            logger.info("Building Docker image `%s`.", user_image_name)
            image_name_or_digest = image_builder.build(
                image_name=user_image_name,
                build_context=build_context,
                docker_build_options=docker_settings.build_options,
                container_registry=container_registry if push else None,
            )

        elif not requires_zenml_build:
            if parent_image == DEFAULT_DOCKER_PARENT_IMAGE:
                raise ValueError(
                    "Unable to run a ZenML pipeline with the given Docker "
                    "settings: No Dockerfile or custom parent image "
                    "specified and no files will be copied or requirements "
                    "installed."
                )
            else:
                # The parent image will be used directly to run the pipeline and
                # needs to be tagged/pushed
                docker_utils.tag_image(parent_image, target=target_image_name)
                if container_registry:
                    image_name_or_digest = container_registry.push_image(
                        target_image_name
                    )
                else:
                    image_name_or_digest = target_image_name

        if requires_zenml_build:
            logger.info("Building Docker image `%s`.", target_image_name)
            # Leave the build context empty if we don't want to include any files
            build_context_root = (
                source_utils.get_source_root() if include_files else None
            )
            build_context = build_context_class(
                root=build_context_root,
                dockerignore_file=docker_settings.dockerignore,
            )

            requirements_files = self.gather_requirements_files(
                docker_settings=docker_settings,
                stack=stack,
                # Only pass code repo to include its dependencies if we actually
                # need to download code
                code_repository=code_repository if download_files else None,
            )

            self._add_requirements_files(
                requirements_files=requirements_files,
                build_context=build_context,
            )
            requirements = (
                "\n".join(
                    file_content for _, file_content, _ in requirements_files
                )
                or None
            )

            apt_packages = docker_settings.apt_packages.copy()
            if docker_settings.install_stack_requirements:
                apt_packages += stack.apt_packages

            if apt_packages:
                logger.info(
                    "Including apt packages: %s",
                    ", ".join(f"`{p}`" for p in apt_packages),
                )

            if parent_image == DEFAULT_DOCKER_PARENT_IMAGE:
                # The default parent image is static and doesn't require a pull
                # each time
                pull_parent_image = False
            elif docker_settings.dockerfile and not container_registry:
                # We built a custom parent image and there was no container
                # registry in the stack to push to, this is a local image
                pull_parent_image = False
            elif not image_builder.is_building_locally:
                # Remote image builders always need to pull the image
                pull_parent_image = True
            else:
                # If the image is local, we don't need to pull it. Otherwise
                # we play it safe and always pull in case the user pushed a new
                # image for the given name and tag
                pull_parent_image = not docker_utils.is_local_image(
                    parent_image
                )

            build_options = {"pull": pull_parent_image, "rm": False}

            dockerfile = self._generate_zenml_pipeline_dockerfile(
                parent_image=parent_image,
                docker_settings=docker_settings,
                download_files=download_files,
                requirements_files=requirements_files,
                apt_packages=apt_packages,
                entrypoint=entrypoint,
            )
            build_context.add_file(destination="Dockerfile", source=dockerfile)

            if extra_files:
                for destination, source in extra_files.items():
                    build_context.add_file(
                        destination=destination, source=source
                    )

            image_name_or_digest = image_builder.build(
                image_name=target_image_name,
                build_context=build_context,
                docker_build_options=build_options,
                container_registry=container_registry,
            )

        return image_name_or_digest, dockerfile, requirements

    @staticmethod
    def _get_target_image_name(
        docker_settings: "DockerSettings",
        tag: str,
        container_registry: Optional["BaseContainerRegistry"] = None,
    ) -> str:
        """Returns the target image name.

        If a container registry is given, the image name will include the
        registry URI

        Args:
            docker_settings: The settings for the image build.
            tag: The tag to use for the image.
            container_registry: Optional container registry to which this
                image will be pushed.

        Returns:
            The docker image name.
        """
        target_image_name = f"{docker_settings.target_repository}:{tag}"
        if container_registry:
            target_image_name = (
                f"{container_registry.config.uri}/{target_image_name}"
            )

        return target_image_name

    @classmethod
    def _add_requirements_files(
        cls,
        requirements_files: List[Tuple[str, str, List[str]]],
        build_context: "BuildContext",
    ) -> None:
        """Adds requirements files to the build context.

        Args:
            requirements_files: List of tuples
                (filename, file_content, pip_options).
            build_context: Build context to add the requirements files to.
        """
        for filename, file_content, _ in requirements_files:
            build_context.add_file(source=file_content, destination=filename)

    @staticmethod
    def gather_requirements_files(
        docker_settings: DockerSettings,
        stack: "Stack",
        code_repository: Optional["BaseCodeRepository"] = None,
        log: bool = True,
    ) -> List[Tuple[str, str, List[str]]]:
        """Gathers and/or generates pip requirements files.

        This method is called in `PipelineDockerImageBuilder.build_docker_image`
        but it is also called by other parts of the codebase, e.g. the
        `AzureMLStepOperator`, which needs to upload the requirements files to
        AzureML where the step image is then built.

        Args:
            docker_settings: Docker settings that specifies which
                requirements to install.
            stack: The stack on which the pipeline will run.
            code_repository: The code repository from which files will be
                downloaded.
            log: If True, will log the requirements.

        Raises:
            RuntimeError: If the command to export the local python packages
                failed.
            FileNotFoundError: If the specified requirements file does not
                exist.

        Returns:
            List of tuples (filename, file_content, pip_options) of all
            requirements files.
            The files will be in the following order:
            - Packages installed in the local Python environment
            - User-defined requirements
            - Requirements defined by user-defined and/or stack integrations
        """
        requirements_files: List[Tuple[str, str, List[str]]] = []

        # Generate requirements file for the local environment if configured
        if docker_settings.replicate_local_python_environment:
            if isinstance(
                docker_settings.replicate_local_python_environment,
                PythonEnvironmentExportMethod,
            ):
                command = (
                    docker_settings.replicate_local_python_environment.command
                )
            else:
                command = " ".join(
                    docker_settings.replicate_local_python_environment
                )

            try:
                local_requirements = subprocess.check_output(
                    command,
                    shell=True,  # nosec
                ).decode()
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    "Unable to export local python packages."
                ) from e

            requirements_files.append(
                (".zenml_local_requirements", local_requirements, [])
            )
            if log:
                logger.info(
                    "- Including python packages from local environment"
                )

        # Generate/Read requirements file for user-defined requirements
        if isinstance(docker_settings.requirements, str):
            path = os.path.abspath(docker_settings.requirements)
            try:
                user_requirements = io_utils.read_file_contents_as_string(path)
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    f"Requirements file {path} does not exist."
                ) from e
            if log:
                logger.info(
                    "- Including user-defined requirements from file `%s`",
                    path,
                )
        elif isinstance(docker_settings.requirements, List):
            user_requirements = "\n".join(docker_settings.requirements)
            if log:
                logger.info(
                    "- Including user-defined requirements: %s",
                    ", ".join(f"`{r}`" for r in docker_settings.requirements),
                )
        else:
            user_requirements = None

        if user_requirements:
            requirements_files.append(
                (".zenml_user_requirements", user_requirements, [])
            )

        # Generate requirements file for all required integrations
        integration_requirements = set(
            itertools.chain.from_iterable(
                integration_registry.select_integration_requirements(
                    integration_name=integration,
                    target_os=OperatingSystemType.LINUX,
                )
                for integration in docker_settings.required_integrations
            )
        )

        if docker_settings.install_stack_requirements:
            integration_requirements.update(stack.requirements())
            if code_repository:
                integration_requirements.update(code_repository.requirements)

        if integration_requirements:
            integration_requirements_list = sorted(integration_requirements)
            integration_requirements_file = "\n".join(
                integration_requirements_list
            )
            requirements_files.append(
                (
                    ".zenml_integration_requirements",
                    integration_requirements_file,
                    [],
                )
            )
            if log:
                logger.info(
                    "- Including integration requirements: %s",
                    ", ".join(f"`{r}`" for r in integration_requirements_list),
                )

        # Generate requirements files for all ZenML Hub plugins
        if docker_settings.required_hub_plugins:
            (
                hub_internal_requirements,
                hub_pypi_requirements,
            ) = PipelineDockerImageBuilder._get_hub_requirements(
                docker_settings.required_hub_plugins
            )

            # Plugin packages themselves
            for i, (index, packages) in enumerate(
                hub_internal_requirements.items()
            ):
                file_name = f".zenml_hub_internal_requirements_{i}"
                file_lines = [f"-i {index}", *packages]
                file_contents = "\n".join(file_lines)
                requirements_files.append(
                    (file_name, file_contents, ["--no-deps"])
                )
                if log:
                    logger.info(
                        "- Including internal hub packages from index `%s`: %s",
                        index,
                        ", ".join(f"`{r}`" for r in packages),
                    )

            # PyPI requirements of plugin packages
            if hub_pypi_requirements:
                file_name = ".zenml_hub_pypi_requirements"
                file_contents = "\n".join(hub_pypi_requirements)
                requirements_files.append((file_name, file_contents, []))
                if log:
                    logger.info(
                        "- Including hub requirements from PyPI: %s",
                        ", ".join(f"`{r}`" for r in hub_pypi_requirements),
                    )

        return requirements_files

    @staticmethod
    def _get_hub_requirements(
        required_hub_plugins: List[str],
    ) -> Tuple[Dict[str, List[str]], List[str]]:
        """Get package requirements for ZenML Hub plugins.

        Args:
            required_hub_plugins: List of hub plugin names in the format
                `(<author_username>/)<plugin_name>(==<version>)`.

        Returns:
            - A dict of the hub plugin packages themselves (which need to be
                installed from a custom index, mapping index URLs to lists of
                package names.
            - A list of all unique dependencies of the required hub plugins
                (which can be installed from PyPI).
        """
        from zenml._hub.client import HubClient
        from zenml._hub.utils import parse_plugin_name, plugin_display_name

        client = HubClient()

        internal_requirements: DefaultDict[str, List[str]] = defaultdict(list)
        pypi_requirements: List[str] = []

        for plugin_str in required_hub_plugins:
            author, name, version = parse_plugin_name(
                plugin_str, version_separator="=="
            )

            plugin = client.get_plugin(
                name=name,
                version=version,
                author=author,
            )

            if plugin and plugin.index_url and plugin.package_name:
                internal_requirements[plugin.index_url].append(
                    plugin.package_name
                )
                if plugin.requirements:
                    pypi_requirements.extend(plugin.requirements)
            else:
                display_name = plugin_display_name(name, version, author)
                logger.warning(
                    "Hub plugin `%s` does not exist or cannot be installed."
                    "Skipping installation of this plugin.",
                    display_name,
                )

        pypi_requirements = sorted(set(pypi_requirements))
        return dict(internal_requirements), pypi_requirements

    @staticmethod
    def _generate_zenml_pipeline_dockerfile(
        parent_image: str,
        docker_settings: DockerSettings,
        download_files: bool,
        requirements_files: Sequence[Tuple[str, str, List[str]]] = (),
        apt_packages: Sequence[str] = (),
        entrypoint: Optional[str] = None,
    ) -> str:
        """Generates a Dockerfile.

        Args:
            parent_image: The image to use as parent for the Dockerfile.
            docker_settings: Docker settings for this image build.
            download_files: Whether to download files in the build context.
            requirements_files: List of tuples that contain three items:
                - the name of a requirements file,
                - the content of that file,
                - options that should be passed to pip when installing the
                    requirements file.
            apt_packages: APT packages to install.
            entrypoint: The default entrypoint command that gets executed when
                running a container of an image created by this Dockerfile.

        Returns:
            The generated Dockerfile.
        """
        lines = [f"FROM {parent_image}", f"WORKDIR {DOCKER_IMAGE_WORKDIR}"]

        for key, value in docker_settings.environment.items():
            lines.append(f"ENV {key.upper()}={value}")

        if apt_packages:
            apt_packages = " ".join(f"'{p}'" for p in apt_packages)

            lines.append(
                "RUN apt-get update && apt-get install -y "
                f"--no-install-recommends {apt_packages}"
            )

        for file, _, options in requirements_files:
            lines.append(f"COPY {file} .")

            option_string = " ".join(options)
            lines.append(
                f"RUN pip install --default-timeout=60 --no-cache-dir "
                f"{option_string} -r {file}"
            )

        lines.append(f"ENV {ENV_ZENML_ENABLE_REPO_INIT_WARNINGS}=False")
        if download_files:
            lines.append(f"ENV {ENV_ZENML_REQUIRES_CODE_DOWNLOAD}=True")

        lines.append(
            f"ENV {ENV_ZENML_CONFIG_PATH}={DOCKER_IMAGE_ZENML_CONFIG_PATH}"
        )

        lines.append("COPY . .")
        lines.append("RUN chmod -R a+rw .")

        if docker_settings.user:
            # Change file ownership to specified user
            lines.append(f"RUN chown -R {docker_settings.user} .")
            # Switch back to specified user for subsequent instructions
            lines.append(f"USER {docker_settings.user}")

        if entrypoint:
            lines.append(f"ENTRYPOINT {entrypoint}")

        return "\n".join(lines)
