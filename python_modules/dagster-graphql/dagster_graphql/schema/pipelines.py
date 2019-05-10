from __future__ import absolute_import

from dagster import (
    ExpectationDefinition,
    LoggerDefinition,
    ModeDefinition,
    PipelineDefinition,
    ResourceDefinition,
    check,
)
from dagster.core.definitions import (
    PipelinePreset,
    create_environment_schema,
    create_environment_type,
)
from dagster_graphql import dauphin

from .config_types import to_dauphin_config_type
from .runtime_types import to_dauphin_runtime_type
from .solids import DauphinSolidContainer, build_dauphin_solid_handles, build_dauphin_solids


class DauphinPipeline(dauphin.ObjectType):
    class Meta:
        name = 'Pipeline'
        interfaces = [DauphinSolidContainer]

    name = dauphin.NonNull(dauphin.String)
    description = dauphin.String()
    solids = dauphin.non_null_list('Solid')
    environment_type = dauphin.Field(
        dauphin.NonNull('ConfigType'), mode=dauphin.String(required=False)
    )
    config_types = dauphin.Field(
        dauphin.non_null_list('ConfigType'), mode=dauphin.String(required=False)
    )
    runtime_types = dauphin.non_null_list('RuntimeType')
    runs = dauphin.non_null_list('PipelineRun')
    modes = dauphin.non_null_list('Mode')
    solid_handles = dauphin.non_null_list('SolidHandle')

    def __init__(self, pipeline):
        super(DauphinPipeline, self).__init__(name=pipeline.name, description=pipeline.description)
        self._pipeline = check.inst_param(pipeline, 'pipeline', PipelineDefinition)

    def resolve_solids(self, _graphene_info):
        return build_dauphin_solids(self._pipeline)

    def resolve_environment_type(self, _graphene_info, mode=None):
        return to_dauphin_config_type(
            create_environment_type(
                self._pipeline,
                # should be able to get rid of get_default_mode_name
                # https://github.com/dagster-io/dagster/issues/1343
                mode if mode else self._pipeline.get_default_mode_name(),
            )
        )

    def resolve_config_types(self, _graphene_info, mode=None):
        # TODO Core UI. Need to rework sidebar
        # https://github.com/dagster-io/dagster/issues/1343
        environment_schema = create_environment_schema(
            self._pipeline, mode if mode else self._pipeline.get_default_mode_name()
        )
        return sorted(
            list(map(to_dauphin_config_type, environment_schema.all_config_types())),
            key=lambda config_type: config_type.key,
        )

    def resolve_runtime_types(self, _graphene_info):
        return sorted(
            list(
                map(
                    to_dauphin_runtime_type,
                    [t for t in self._pipeline.all_runtime_types() if t.name],
                )
            ),
            key=lambda config_type: config_type.name,
        )

    def resolve_runs(self, graphene_info):
        return [
            graphene_info.schema.type_named('PipelineRun')(r)
            for r in graphene_info.context.pipeline_runs.all_runs_for_pipeline(self._pipeline.name)
        ]

    def get_dagster_pipeline(self):
        return self._pipeline

    def get_type(self, _graphene_info, typeName):
        if self._pipeline.has_config_type(typeName):
            return to_dauphin_config_type(self._pipeline.config_type_named(typeName))
        elif self._pipeline.has_runtime_type(typeName):
            return to_dauphin_runtime_type(self._pipeline.runtime_type_named(typeName))

        else:
            check.failed('Not a config type or runtime type')

    def resolve_modes(self, graphene_info):
        return sorted(
            [
                graphene_info.schema.type_named('Mode')(mode_definition)
                for mode_definition in self._pipeline.mode_definitions
            ],
            key=lambda item: item.name,
        )

    def resolve_solid_handles(self, _graphene_info):
        return sorted(
            build_dauphin_solid_handles(self._pipeline), key=lambda item: str(item.handleID)
        )


class DauphinPipelineConnection(dauphin.ObjectType):
    class Meta:
        name = 'PipelineConnection'

    nodes = dauphin.non_null_list('Pipeline')


class DauphinResource(dauphin.ObjectType):
    class Meta:
        name = 'Resource'

    def __init__(self, resource_name, resource):
        self.name = check.str_param(resource_name, 'resource_name')
        self._resource = check.inst_param(resource, 'resource', ResourceDefinition)
        self.description = resource.description

    name = dauphin.NonNull(dauphin.String)
    description = dauphin.String()
    configField = dauphin.Field('ConfigTypeField')

    def resolve_configField(self, graphene_info):
        return (
            graphene_info.schema.type_named('ConfigTypeField')(
                name="config", field=self._resource.config_field
            )
            if self._resource.config_field
            else None
        )


class DauphinLogger(dauphin.ObjectType):
    class Meta:
        name = 'Logger'

    def __init__(self, logger_name, logger):
        self.name = check.str_param(logger_name, 'logger_name')
        self._logger = check.inst_param(logger, 'logger', LoggerDefinition)
        self.description = logger.description

    name = dauphin.NonNull(dauphin.String)
    description = dauphin.String()
    configField = dauphin.Field('ConfigTypeField')

    def resolve_configField(self, graphene_info):
        return (
            graphene_info.schema.type_named('ConfigTypeField')(
                name="config", field=self._logger.config_field
            )
            if self._logger.config_field
            else None
        )


class DauphinMode(dauphin.ObjectType):
    def __init__(self, mode_definition):
        self._mode_definition = check.inst_param(mode_definition, 'mode_definition', ModeDefinition)

    class Meta:
        name = 'Mode'

    name = dauphin.NonNull(dauphin.String)
    description = dauphin.String()
    resources = dauphin.non_null_list('Resource')
    loggers = dauphin.non_null_list('Logger')

    def resolve_name(self, _graphene_info):
        return self._mode_definition.name

    def resolve_description(self, _graphene_info):
        return self._mode_definition.description

    def resolve_resources(self, _graphene_info):
        return [
            DauphinResource(*item) for item in sorted(self._mode_definition.resource_defs.items())
        ]

    def resolve_loggers(self, _graphene_info):
        return [DauphinLogger(*item) for item in sorted(self._mode_definition.loggers.items())]


class DauphinMetadataItemDefinition(dauphin.ObjectType):
    class Meta:
        name = 'MetadataItemDefinition'

    key = dauphin.NonNull(dauphin.String)
    value = dauphin.NonNull(dauphin.String)


class DauphinExpectation(dauphin.ObjectType):
    class Meta:
        name = 'Expectation'

    name = dauphin.NonNull(dauphin.String)
    description = dauphin.String()

    def __init__(self, expectation):
        check.inst_param(expectation, 'expectation', ExpectationDefinition)
        super(DauphinExpectation, self).__init__(
            name=expectation.name, description=expectation.description
        )


class DauphinPipelinePreset(dauphin.ObjectType):
    class Meta:
        name = 'PipelinePreset'

    name = dauphin.NonNull(dauphin.String)
    solidSubset = dauphin.List(dauphin.NonNull(dauphin.String))
    environment = dauphin.String()

    def __init__(self, preset):
        self.preset = check.inst_param(preset, 'preset', PipelinePreset)

    def resolve_name(self, _graphene_info):
        return self.preset.name

    def resolve_solidSubset(self, _graphene_info):
        return self.preset.solid_subset

    def resolve_environment(self, _graphene_info):
        return self.preset.environment_yaml
