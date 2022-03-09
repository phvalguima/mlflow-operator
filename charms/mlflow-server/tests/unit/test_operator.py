# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from base64 import b64decode

import pytest
import yaml
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Harness

from charm import Operator


@pytest.fixture
def harness():
    return Harness(Operator)


def test_not_leader(harness):
    harness.begin_with_initial_hooks()
    assert isinstance(harness.charm.model.unit.status, WaitingStatus)


def test_missing_image(harness):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    assert isinstance(harness.charm.model.unit.status, BlockedStatus)


def test_main_no_relation(harness):
    harness.set_leader(True)
    harness.add_oci_resource(
        "oci-image",
        {
            "registrypath": "ci-test",
            "username": "",
            "password": "",
        },
    )
    harness.begin_with_initial_hooks()
    pod_spec = harness.get_pod_spec()
    # confirm that we can serialize the pod spec
    yaml.safe_dump(pod_spec)

    assert harness.charm.model.unit.status == WaitingStatus("Waiting for mysql relation data")


def test_install_with_all_inputs(harness):
    harness.set_leader(True)
    harness.add_oci_resource(
        "oci-image",
        {
            "registrypath": "ci-test",
            "username": "",
            "password": "",
        },
    )

    # mysql relation data
    mysql_data = {
        "database": "database",
        "host": "host",
        "root_password": "lorem-ipsum",
        "port": "port",
    }
    rel_id = harness.add_relation("db", "mysql_app")
    harness.add_relation_unit(rel_id, "mysql_app/0")
    harness.update_relation_data(rel_id, "mysql_app/0", mysql_data)

    # object storage

    os_data = {
        "access-key": "minio-access-key",
        "namespace": "namespace",
        "port": 1234,
        "secret-key": "minio-super-secret-key",
        "secure": True,
        "service": "service",
    }
    os_rel_data = {
        "_supported_versions": "- v1",
        "data": yaml.dump(os_data),
    }
    os_rel_id = harness.add_relation("object-storage", "storage-provider")
    harness.add_relation_unit(os_rel_id, "storage-provider/0")
    harness.update_relation_data(os_rel_id, "storage-provider", os_rel_data)

    # ingress
    ingress_relation_name = "ingress"
    relation_version_data = {"_supported_versions": "- v1"}
    ingress_rel_id = harness.add_relation(
        ingress_relation_name, f"{ingress_relation_name}-subscriber"
    )
    harness.add_relation_unit(ingress_rel_id, f"{ingress_relation_name}-subscriber/0")
    harness.update_relation_data(
        ingress_rel_id, f"{ingress_relation_name}-subscriber", relation_version_data
    )

    # pod defaults relations setup
    pod_defaults_rel_name = "pod-defaults"
    pod_defaults_rel_id = harness.add_relation(
        "pod-defaults", f"{pod_defaults_rel_name}-subscriber"
    )
    harness.add_relation_unit(pod_defaults_rel_id, f"{pod_defaults_rel_name}-subscriber/0")

    harness.begin_with_initial_hooks()

    pod_spec = harness.get_pod_spec()
    yaml.safe_dump(pod_spec)
    assert harness.charm.model.unit.status == ActiveStatus()

    charm_name = harness.model.app.name
    secrets = pod_spec[0]["kubernetesResources"]["secrets"]
    env_config = pod_spec[0]["containers"][0]["envConfig"]
    minio_secrets = [s for s in secrets if s["name"] == f"{charm_name}-minio-secret"][0]
    db_secrets = [s for s in secrets if s["name"] == f"{charm_name}-db-secret"][0]

    assert env_config["db-secret"]["secret"]["name"] == db_secrets["name"]
    assert b64decode(db_secrets["data"]["DB_ROOT_PASSWORD"]).decode("utf-8") == "lorem-ipsum"
    assert b64decode(db_secrets["data"]["MLFLOW_TRACKING_URI"]).decode(
        "utf-8"
    ) == "mysql+pymysql://{}:{}@{}:{}/{}".format(
        "root",
        mysql_data["root_password"],
        mysql_data["host"],
        mysql_data["port"],
        mysql_data["database"],
    )

    assert env_config["aws-secret"]["secret"]["name"] == minio_secrets["name"]
    assert (
        b64decode(minio_secrets["data"]["AWS_ACCESS_KEY_ID"]).decode("utf-8") == "minio-access-key"
    )
    assert (
        b64decode(minio_secrets["data"]["AWS_SECRET_ACCESS_KEY"]).decode("utf-8")
        == "minio-super-secret-key"
    )

    # test correct data structure is sent to admission webhook

    mlflow_pod_defaults_data = {
        key.name: value
        for key, value in harness.model.get_relation(
            pod_defaults_rel_name, pod_defaults_rel_id
        ).data.items()
        if "mlflow-server" in key.name
    }
    mlflow_pod_defaults_minio_data = json.loads(
        mlflow_pod_defaults_data[charm_name]["pod-defaults"]
    )["minio"]["env"]

    assert mlflow_pod_defaults_minio_data["AWS_ACCESS_KEY_ID"] == os_data["access-key"]
    assert mlflow_pod_defaults_minio_data["AWS_SECRET_ACCESS_KEY"] == os_data["secret-key"]
    assert (
        mlflow_pod_defaults_minio_data["MLFLOW_S3_ENDPOINT_URL"]
        == f"http://{os_data['service']}:{os_data['port']}"
    )
    assert (
        mlflow_pod_defaults_minio_data["MLFLOW_TRACKING_URI"]
        == f"http://{harness.model.app.name}.{harness.model.name}.svc.cluster.local:{harness.charm.config['mlflow_port']}"
    )
    assert "requirements" in mlflow_pod_defaults_data[f"{charm_name}/0"]
