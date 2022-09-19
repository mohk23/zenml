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
"""MLFlow model deployer flavor."""

from typing import TYPE_CHECKING, Type

from zenml.integrations.mlflow import MLFLOW_MODEL_DEPLOYER_FLAVOR
from zenml.model_deployers.base_model_deployer import (
    BaseModelDeployerConfig,
    BaseModelDeployerFlavor,
)

if TYPE_CHECKING:
    from zenml.integrations.mlflow.model_deployers import MLFlowModelDeployer


class MLFlowModelDeployerConfig(BaseModelDeployerConfig):
    """Configuration for the MLflow model deployer.

    Attributes:
        service_path: the path where the local MLflow deployment service
            configuration, PID and log files are stored.
    """

    service_path: str = ""


class MLFlowModelDeployerFlavor(BaseModelDeployerFlavor):
    """Model deployer flavor for MLFlow models."""

    @property
    def name(self) -> str:
        return MLFLOW_MODEL_DEPLOYER_FLAVOR

    @property
    def config_class(self) -> Type[MLFlowModelDeployerConfig]:
        return MLFlowModelDeployerConfig

    @property
    def implementation_class(self) -> Type["MLFlowModelDeployer"]:
        from zenml.integrations.mlflow.model_deployers import (
            MLFlowModelDeployer,
        )

        return MLFlowModelDeployer
