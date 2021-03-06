from dagster.core.scheduler.job import JobStatus
from dagster_graphql.test.utils import (
    execute_dagster_graphql,
    infer_repository_selector,
    infer_sensor_selector,
)

from .graphql_context_test_suite import (
    ExecutingGraphQLContextTestMatrix,
    ReadonlyGraphQLContextTestMatrix,
)

GET_SENSORS_QUERY = """
query SensorsQuery($repositorySelector: RepositorySelector!) {
  sensorsOrError(repositorySelector: $repositorySelector) {
    __typename
    ... on PythonError {
      message
      stack
    }
    ... on Sensors {
      results {
        id
        name
        pipelineName
        solidSelection
        mode
        sensorState {
          status
          runs {
              id
              runId
          }
          runsCount
          ticks {
              id
              status
              timestamp
              runIds
              error {
                  message
                  stack
              }
          }
        }
      }
    }
  }
}
"""

GET_SENSOR_QUERY = """
query SensorQuery($sensorSelector: SensorSelector!) {
  sensorOrError(sensorSelector: $sensorSelector) {
    __typename
    ... on PythonError {
      message
      stack
    }
    ... on Sensor {
      id
      name
      pipelineName
      solidSelection
      mode
      sensorState {
        status
        runs {
          id
          runId
        }
        runsCount
        ticks {
            id
            status
            timestamp
            runIds
            error {
                message
                stack
            }
        }
      }
    }
  }
}
"""

START_SENSORS_QUERY = """
mutation($sensorSelector: SensorSelector!) {
  startSensor(sensorSelector: $sensorSelector) {
    ... on PythonError {
      message
      className
      stack
    }
    ... on Sensor {
      id
      jobOriginId
      sensorState {
        status
      }
    }
  }
}
"""

STOP_SENSORS_QUERY = """
mutation($jobOriginId: String!) {
  stopSensor(jobOriginId: $jobOriginId) {
    ... on PythonError {
      message
      className
      stack
    }
    ... on StopSensorMutationResult {
      jobState {
        status
      }
    }
  }
}
"""


class TestSensors(ReadonlyGraphQLContextTestMatrix):
    def test_get_sensors(self, graphql_context, snapshot):
        selector = infer_repository_selector(graphql_context)
        result = execute_dagster_graphql(
            graphql_context, GET_SENSORS_QUERY, variables={"repositorySelector": selector},
        )

        assert result.data
        assert result.data["sensorsOrError"]
        assert result.data["sensorsOrError"]["__typename"] == "Sensors"
        results = result.data["sensorsOrError"]["results"]
        snapshot.assert_match(results)

    def test_get_sensor(self, graphql_context, snapshot):
        sensor_selector = infer_sensor_selector(graphql_context, "always_no_config_sensor")
        result = execute_dagster_graphql(
            graphql_context, GET_SENSOR_QUERY, variables={"sensorSelector": sensor_selector},
        )

        assert result.data
        assert result.data["sensorOrError"]
        assert result.data["sensorOrError"]["__typename"] == "Sensor"
        sensor = result.data["sensorOrError"]
        snapshot.assert_match(sensor)


class TestSensorMutations(ExecutingGraphQLContextTestMatrix):
    def test_start_sensor(self, graphql_context):
        sensor_selector = infer_sensor_selector(graphql_context, "always_no_config_sensor")
        result = execute_dagster_graphql(
            graphql_context, START_SENSORS_QUERY, variables={"sensorSelector": sensor_selector},
        )
        assert result.data

        assert result.data["startSensor"]["sensorState"]["status"] == JobStatus.RUNNING.value

    def test_stop_sensor(self, graphql_context):
        sensor_selector = infer_sensor_selector(graphql_context, "always_no_config_sensor")

        # start sensor
        start_result = execute_dagster_graphql(
            graphql_context, START_SENSORS_QUERY, variables={"sensorSelector": sensor_selector},
        )
        assert start_result.data["startSensor"]["sensorState"]["status"] == JobStatus.RUNNING.value

        job_origin_id = start_result.data["startSensor"]["jobOriginId"]
        result = execute_dagster_graphql(
            graphql_context, STOP_SENSORS_QUERY, variables={"jobOriginId": job_origin_id},
        )
        assert result.data
        assert result.data["stopSensor"]["jobState"]["status"] == JobStatus.STOPPED.value
