from dagster import check
from dagster.core.definitions import (
    AssetMaterialization,
    ExpectationResult,
    Failure,
    Materialization,
    Output,
    RetryRequested,
    TypeCheck,
)
from dagster.core.definitions.events import (
    AssetStoreOperation,
    AssetStoreOperationType,
    ObjectStoreOperation,
)
from dagster.core.errors import (
    DagsterExecutionStepExecutionError,
    DagsterInvariantViolationError,
    DagsterStepOutputNotFoundError,
    DagsterTypeCheckDidNotPass,
    DagsterTypeCheckError,
    DagsterTypeMaterializationError,
    user_code_error_boundary,
)
from dagster.core.events import DagsterEvent
from dagster.core.execution.context.system import SystemStepExecutionContext
from dagster.core.execution.plan.objects import (
    StepInputData,
    StepOutputData,
    StepOutputHandle,
    StepSuccessData,
    TypeCheckData,
)
from dagster.core.types.dagster_type import DagsterTypeKind
from dagster.utils import ensure_gen, iterate_with_context, raise_interrupts_immediately
from dagster.utils.timing import time_execution_scope

from .inputs import FanInStepInputValuesWrapper


def _step_output_error_checked_user_event_sequence(step_context, user_event_sequence):
    """
    Process the event sequence to check for invariant violations in the event
    sequence related to Output events emitted from the compute_fn.

    This consumes and emits an event sequence.
    """
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.generator_param(user_event_sequence, "user_event_sequence")

    step = step_context.step
    output_names = list([output_def.name for output_def in step.step_outputs])
    seen_outputs = set()

    for user_event in user_event_sequence:
        if not isinstance(user_event, Output):
            yield user_event
            continue

        # do additional processing on Outputs
        output = user_event
        if not step.has_step_output(output.output_name):
            raise DagsterInvariantViolationError(
                'Core compute for solid "{handle}" returned an output '
                '"{output.output_name}" that does not exist. The available '
                "outputs are {output_names}".format(
                    handle=str(step.solid_handle), output=output, output_names=output_names
                )
            )

        if output.output_name in seen_outputs:
            raise DagsterInvariantViolationError(
                'Core compute for solid "{handle}" returned an output '
                '"{output.output_name}" multiple times'.format(
                    handle=str(step.solid_handle), output=output
                )
            )

        yield output
        seen_outputs.add(output.output_name)

    for step_output in step.step_outputs:
        step_output_def = step_output.output_def
        if not step_output_def.name in seen_outputs and not step_output_def.optional:
            if step_output_def.dagster_type.kind == DagsterTypeKind.NOTHING:
                step_context.log.info(
                    'Emitting implicit Nothing for output "{output}" on solid {solid}'.format(
                        output=step_output_def.name, solid={str(step.solid_handle)}
                    )
                )
                yield Output(output_name=step_output_def.name, value=None)
            else:
                raise DagsterStepOutputNotFoundError(
                    'Core compute for solid "{handle}" did not return an output '
                    'for non-optional output "{step_output_def.name}"'.format(
                        handle=str(step.solid_handle), step_output_def=step_output_def
                    ),
                    step_key=step.key,
                    output_name=step_output_def.name,
                )


def _do_type_check(context, dagster_type, value):
    type_check = dagster_type.type_check(context, value)
    if not isinstance(type_check, TypeCheck):
        return TypeCheck(
            success=False,
            description=(
                "Type checks must return TypeCheck. Type check for type {type_name} returned "
                "value of type {return_type} when checking runtime value of type {dagster_type}."
            ).format(
                type_name=dagster_type.display_name,
                return_type=type(type_check),
                dagster_type=type(value),
            ),
        )
    return type_check


def _create_step_input_event(step_context, input_name, type_check, success):
    return DagsterEvent.step_input_event(
        step_context,
        StepInputData(
            input_name=input_name,
            type_check_data=TypeCheckData(
                success=success,
                label=input_name,
                description=type_check.description if type_check else None,
                metadata_entries=type_check.metadata_entries if type_check else [],
            ),
        ),
    )


def _type_checked_event_sequence_for_input(step_context, input_name, input_value):
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.str_param(input_name, "input_name")

    step_input = step_context.step.step_input_named(input_name)
    with user_code_error_boundary(
        DagsterTypeCheckError,
        lambda: (
            'In solid "{handle}" the input "{input_name}" received '
            "value {input_value} of Python type {input_type} which "
            "does not pass the typecheck for Dagster type "
            "{dagster_type_name}. Step {step_key}."
        ).format(
            handle=str(step_context.step.solid_handle),
            input_name=input_name,
            input_value=input_value,
            input_type=type(input_value),
            dagster_type_name=step_input.dagster_type.display_name,
            step_key=step_context.step.key,
        ),
    ):
        type_check = _do_type_check(
            step_context.for_type(step_input.dagster_type), step_input.dagster_type, input_value,
        )

        yield _create_step_input_event(
            step_context, input_name, type_check=type_check, success=type_check.success
        )

        if not type_check.success:
            raise DagsterTypeCheckDidNotPass(
                description='Type check failed for step input "{input_name}" - expected type "{dagster_type}".'.format(
                    input_name=input_name, dagster_type=step_input.dagster_type.display_name,
                ),
                metadata_entries=type_check.metadata_entries,
                dagster_type=step_input.dagster_type,
            )


def _create_step_output_event(step_context, output, type_check, success, version):
    return DagsterEvent.step_output_event(
        step_context=step_context,
        step_output_data=StepOutputData(
            step_output_handle=StepOutputHandle.from_step(
                step=step_context.step, output_name=output.output_name
            ),
            type_check_data=TypeCheckData(
                success=success,
                label=output.output_name,
                description=type_check.description if type_check else None,
                metadata_entries=type_check.metadata_entries if type_check else [],
            ),
            version=version,
        ),
    )


def _type_checked_step_output_event_sequence(step_context, output, version):
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.inst_param(output, "output", Output)

    step_output = step_context.step.step_output_named(output.output_name)
    dagster_type = step_output.output_def.dagster_type
    with user_code_error_boundary(
        DagsterTypeCheckError,
        lambda: (
            'In solid "{handle}" the output "{output_name}" received '
            "value {output_value} of Python type {output_type} which "
            "does not pass the typecheck for Dagster type "
            "{dagster_type_name}. Step {step_key}."
        ).format(
            handle=str(step_context.step.solid_handle),
            output_name=output.output_name,
            output_value=output.value,
            output_type=type(output.value),
            dagster_type_name=dagster_type.display_name,
            step_key=step_context.step.key,
        ),
    ):
        type_check = _do_type_check(step_context.for_type(dagster_type), dagster_type, output.value)

        yield _create_step_output_event(
            step_context,
            output,
            type_check=type_check,
            success=type_check.success,
            version=version,
        )

        if not type_check.success:
            raise DagsterTypeCheckDidNotPass(
                description='Type check failed for step output "{output_name}" - expected type "{dagster_type}".'.format(
                    output_name=output.output_name, dagster_type=dagster_type.display_name,
                ),
                metadata_entries=type_check.metadata_entries,
                dagster_type=dagster_type,
            )


def core_dagster_event_sequence_for_step(step_context, prior_attempt_count):
    """
    Execute the step within the step_context argument given the in-memory
    events. This function yields a sequence of DagsterEvents, but without
    catching any exceptions that have bubbled up during the computation
    of the step.
    """
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.int_param(prior_attempt_count, "prior_attempt_count")
    if prior_attempt_count > 0:
        yield DagsterEvent.step_restarted_event(step_context, prior_attempt_count)
    else:
        yield DagsterEvent.step_start_event(step_context)

    inputs = {}
    for input_name, input_value in _load_input_values(step_context):
        if isinstance(input_value, ObjectStoreOperation):
            yield DagsterEvent.object_store_operation(
                step_context, ObjectStoreOperation.serializable(input_value, value_name=input_name)
            )
            inputs[input_name] = input_value.obj
        elif isinstance(input_value, FanInStepInputValuesWrapper):
            final_values = []
            for inner_value in input_value:
                # inner value is either a store interaction
                if isinstance(inner_value, ObjectStoreOperation):
                    yield DagsterEvent.object_store_operation(
                        step_context,
                        ObjectStoreOperation.serializable(inner_value, value_name=input_name),
                    )
                    final_values.append(inner_value.obj)
                elif isinstance(inner_value, AssetStoreOperation):
                    yield DagsterEvent.asset_store_operation(step_context, inner_value)
                    final_values.append(inner_value.obj)
                # or the value directly
                else:
                    final_values.append(inner_value)

            inputs[input_name] = final_values
        elif isinstance(input_value, AssetStoreOperation):
            yield DagsterEvent.asset_store_operation(step_context, input_value)
            inputs[input_name] = input_value.obj
        else:
            inputs[input_name] = input_value

    for input_name, input_value in inputs.items():
        for evt in check.generator(
            _type_checked_event_sequence_for_input(step_context, input_name, input_value)
        ):
            yield evt

    with time_execution_scope() as timer_result:
        user_event_sequence = check.generator(
            _user_event_sequence_for_step_compute_fn(step_context, inputs)
        )

        # It is important for this loop to be indented within the
        # timer block above in order for time to be recorded accurately.
        for user_event in check.generator(
            _step_output_error_checked_user_event_sequence(step_context, user_event_sequence)
        ):

            if isinstance(user_event, Output):
                for evt in _create_step_events_for_output(step_context, user_event):
                    yield evt
            elif isinstance(user_event, (AssetMaterialization, Materialization)):
                yield DagsterEvent.step_materialization(step_context, user_event)
            elif isinstance(user_event, ExpectationResult):
                yield DagsterEvent.step_expectation_result(step_context, user_event)
            else:
                check.failed(
                    "Unexpected event {event}, should have been caught earlier".format(
                        event=user_event
                    )
                )

    yield DagsterEvent.step_success_event(
        step_context, StepSuccessData(duration_ms=timer_result.millis)
    )


def _create_step_events_for_output(step_context, output):

    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.inst_param(output, "output", Output)

    step = step_context.step
    step_output = step.step_output_named(output.output_name)

    version = step_context.execution_plan.resolve_step_output_versions()[
        StepOutputHandle(step_context.step.key, output.output_name)
    ]

    for output_event in _type_checked_step_output_event_sequence(step_context, output, version):
        yield output_event

    step_output_handle = StepOutputHandle.from_step(step=step, output_name=output.output_name)

    for evt in _set_objects(step_context, step_output, step_output_handle, output, version):
        yield evt

    for evt in _create_output_materializations(step_context, output.output_name, output.value):
        yield evt


def _materializations_to_events(step_context, step_output_handle, materializations):
    if materializations is not None:
        for materialization in ensure_gen(materializations):
            if not isinstance(materialization, AssetMaterialization):
                raise DagsterInvariantViolationError(
                    (
                        "asset_store on output {output_name} has returned "
                        "value {value} of type {python_type}. The return type can only be "
                        "AssetMaterialization."
                    ).format(
                        output_name=step_output_handle.output_name,
                        value=repr(materialization),
                        python_type=type(materialization).__name__,
                    )
                )

            yield DagsterEvent.step_materialization(step_context, materialization)


def _set_objects(step_context, step_output, step_output_handle, output, version):
    from dagster.core.storage.asset_store import AssetStoreHandle

    output_def = step_output.output_def
    if step_context.using_asset_store(step_output_handle):
        output_manager = getattr(step_context.resources, output_def.manager_key)
        output_context = step_context.get_output_context(step_output_handle)
        materializations = output_manager.handle_output(output_context, output.value)

        for evt in _materializations_to_events(step_context, step_output_handle, materializations):
            yield evt

        # SET_ASSET operation by AssetStore
        yield DagsterEvent.asset_store_operation(
            step_context,
            AssetStoreOperation(
                AssetStoreOperationType.SET_ASSET,
                step_output_handle,
                AssetStoreHandle(output_def.manager_key, output_def.metadata),
            ),
        )
    else:
        res = step_context.intermediate_storage.set_intermediate(
            context=step_context,
            dagster_type=step_output.output_def.dagster_type,
            step_output_handle=step_output_handle,
            value=output.value,
            version=version,
        )

        if isinstance(res, ObjectStoreOperation):
            yield DagsterEvent.object_store_operation(
                step_context, ObjectStoreOperation.serializable(res, value_name=output.output_name),
            )


def _create_output_materializations(step_context, output_name, value):
    step = step_context.step
    current_handle = step.solid_handle

    # check for output mappings at every point up the composition hierarchy
    while current_handle:
        solid_config = step_context.environment_config.solids.get(current_handle.to_string())
        current_handle = current_handle.parent

        if solid_config is None:
            continue

        for output_spec in solid_config.outputs.type_materializer_specs:
            check.invariant(len(output_spec) == 1)
            config_output_name, output_spec = list(output_spec.items())[0]
            if config_output_name == output_name:
                step_output = step.step_output_named(output_name)
                with user_code_error_boundary(
                    DagsterTypeMaterializationError,
                    msg_fn=lambda: """Error occurred during output materialization:
                    output name: "{output_name}"
                    step key: "{key}"
                    solid invocation: "{solid}"
                    solid definition: "{solid_def}"
                    """.format(
                        output_name=output_name,
                        key=step_context.step.key,
                        solid_def=step_context.solid_def.name,
                        solid=step_context.solid.name,
                    ),
                ):
                    dagster_type = step_output.output_def.dagster_type
                    materializations = dagster_type.materializer.materialize_runtime_values(
                        step_context, output_spec, value
                    )

                for materialization in materializations:
                    if not isinstance(materialization, (AssetMaterialization, Materialization)):
                        raise DagsterInvariantViolationError(
                            (
                                "materialize_runtime_values on type {type_name} has returned "
                                "value {value} of type {python_type}. You must return an "
                                "AssetMaterialization."
                            ).format(
                                type_name=dagster_type.display_name,
                                value=repr(materialization),
                                python_type=type(materialization).__name__,
                            )
                        )

                    yield DagsterEvent.step_materialization(step_context, materialization)


def _user_event_sequence_for_step_compute_fn(step_context, evaluated_inputs):
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.dict_param(evaluated_inputs, "evaluated_inputs", key_type=str)

    with user_code_error_boundary(
        DagsterExecutionStepExecutionError,
        control_flow_exceptions=[Failure, RetryRequested],
        msg_fn=lambda: """Error occurred during the execution of step:
        step key: "{key}"
        solid invocation: "{solid}"
        solid definition: "{solid_def}"
        """.format(
            key=step_context.step.key,
            solid_def=step_context.solid_def.name,
            solid=step_context.solid.name,
        ),
        step_key=step_context.step.key,
        solid_def_name=step_context.solid_def.name,
        solid_name=step_context.solid.name,
    ):
        gen = check.opt_generator(step_context.step.compute_fn(step_context, evaluated_inputs))
        if not gen:
            return

        # Allow interrupts again during each step of the execution
        for event in iterate_with_context(raise_interrupts_immediately, gen):
            yield event


def _generate_error_boundary_msg_for_step_input(context, input_name):
    return lambda: """Error occurred during input loading:
    input name: "{input_name}"
    step key: "{key}"
    solid invocation: "{solid}"
    solid definition: "{solid_def}"
    """.format(
        input_name=input_name,
        key=context.step.key,
        solid_def=context.solid_def.name,
        solid=context.solid.name,
    )


def _load_input_values(step_context):
    step = step_context.step

    for step_input in step.step_inputs:
        if step_input.dagster_type.kind == DagsterTypeKind.NOTHING:
            continue

        yield step_input.name, step_input.source.load_input_object(step_context)
