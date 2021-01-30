#  Copyright (c) maiot GmbH 2020. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.

from typing import Dict, Text, Any, Optional, List

from tfx.components.bulk_inferrer.component import BulkInferrer
from tfx.components.common_nodes.importer_node import ImporterNode
from tfx.proto import bulk_inferrer_pb2
from tfx.types import standard_artifacts

from zenml.core.backends.orchestrator.base.orchestrator_base_backend import \
    OrchestratorBaseBackend
from zenml.core.components.data_gen.component import DataGen
from zenml.core.datasources.base_datasource import BaseDatasource
from zenml.core.metadata.metadata_wrapper import ZenMLMetadataStore
from zenml.core.pipelines.base_pipeline import BasePipeline
from zenml.core.repo.artifact_store import ArtifactStore
from zenml.core.standards import standard_keys as keys
from zenml.core.standards.standard_keys import StepKeys
from zenml.core.steps.base_step import BaseStep
from zenml.utils.enums import GDPComponent


class BatchInferencePipeline(BasePipeline):
    """BatchInferencePipeline definition to run batch inference pipelines.

    A BatchInferencePipeline is used to run an inference based on a
    TrainingPipeline.
    """
    PIPELINE_TYPE = 'infer'

    def __init__(self,
                 model_uri: Text,
                 name: Text = None,
                 enable_cache: Optional[bool] = True,
                 steps_dict: Dict[Text, BaseStep] = None,
                 backend: OrchestratorBaseBackend = None,
                 metadata_store: Optional[ZenMLMetadataStore] = None,
                 artifact_store: Optional[ArtifactStore] = None,
                 datasource: Optional[BaseDatasource] = None,
                 pipeline_name: Optional[Text] = None):
        """
        Construct a Batch Inference pipeline. This is a pipeline that allows
        for offline batch inference.

        Args:
            name: Outward-facing name of the pipeline.
            model_uri: URI for a model, usually generated by
            TrainingPipeline and retrieved by
            `training_pipeline.get_model_uri()`.
            pipeline_name: A unique name that identifies the pipeline after
             it is run.
            enable_cache: Boolean, indicates whether or not caching
             should be used.
            steps_dict: Optional dict of steps.
            backend: Orchestrator backend.
            metadata_store: Configured metadata store. If None,
             the default metadata store is used.
            artifact_store: Configured artifact store. If None,
             the default artifact store is used.
        """
        if model_uri is None:
            raise AssertionError('model_uri cannot be None.')
        self.model_uri = model_uri
        super(BatchInferencePipeline, self).__init__(
            name=name,
            enable_cache=enable_cache,
            steps_dict=steps_dict,
            backend=backend,
            metadata_store=metadata_store,
            artifact_store=artifact_store,
            datasource=datasource,
            pipeline_name=pipeline_name,
            model_uri=model_uri,
        )

    def get_tfx_component_list(self, config: Dict[Text, Any]) -> List:
        """
        Creates an inference pipeline out of TFX components.

        A inference pipeline is used to run a batch of data through a
        ML model via the BulkInferrer TFX component.

        Args:
            config: Dict. Contains a ZenML configuration used to build the
             data pipeline.

        Returns:
            A list of TFX components making up the data pipeline.
        """
        component_list = []

        data_config = \
            config[keys.GlobalKeys.PIPELINE][keys.PipelineKeys.STEPS][
                keys.DataSteps.DATA]
        data = DataGen(
            name=self.datasource.name,
            source=data_config[StepKeys.SOURCE],
            source_args=data_config[StepKeys.ARGS]).with_id(
            GDPComponent.DataGen.name)
        component_list.extend([data])

        # Handle timeseries
        # TODO: [LOW] Handle timeseries
        # if GlobalKeys. in train_config:
        #     schema = ImporterNode(instance_name='Schema',
        #                           source_uri=spec['schema_uri'],
        #                           artifact_type=standard_artifacts.Schema)
        #
        #     sequence_transform = SequenceTransform(
        #         examples=data.outputs.examples,
        #         schema=schema,
        #         config=train_config,
        #         instance_name=GDPComponent.SequenceTransform.name)
        #     datapoints = sequence_transform.outputs.output
        #     component_list.extend([schema, sequence_transform])

        # Load from model_uri
        model = ImporterNode(
            instance_name=GDPComponent.Trainer.name,
            source_uri=self.model_uri,
            artifact_type=standard_artifacts.Model)
        model_result = model.outputs.result

        bulk_inferrer = BulkInferrer(
            examples=data.outputs.examples,
            model=model_result,
            instance_name=GDPComponent.Inferrer.name,
            # Empty data_spec.example_splits will result in using all splits.
            data_spec=bulk_inferrer_pb2.DataSpec(),
            model_spec=bulk_inferrer_pb2.ModelSpec())

        component_list.extend([model, bulk_inferrer])

        return component_list

    def steps_completed(self) -> bool:
        mandatory_steps = [keys.DataSteps.DATA]
        for step_name in mandatory_steps:
            if step_name not in self.steps_dict.keys():
                raise AssertionError(
                    f'Mandatory step {step_name} not added.')
        return True
