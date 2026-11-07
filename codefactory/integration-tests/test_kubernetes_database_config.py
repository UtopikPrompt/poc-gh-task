import re
import subprocess
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBERNETES_ROOT = REPOSITORY_ROOT / "infra-codefactory/kubernetes"
INFERENCE_KUBERNETES_ROOT = REPOSITORY_ROOT / "inference-platform/kubernetes"


def _render_environment(environment: str) -> list[dict[str, object]]:
    rendered = subprocess.run(
        [
            "kubectl",
            "kustomize",
            str(KUBERNETES_ROOT / "environments" / environment),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return list(yaml.safe_load_all(rendered))


def test_inference_kustomizations_publish_templates_not_live_workloads():
    paths = (
        INFERENCE_KUBERNETES_ROOT,
        INFERENCE_KUBERNETES_ROOT / "inference-serving/base",
        INFERENCE_KUBERNETES_ROOT / "inference-serving/cpu",
    )
    workload_names = {"llama-cpp-server", "llama-cpp-server-cpu"}

    for path in paths:
        rendered = subprocess.run(
            ["kubectl", "kustomize", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        resources = list(yaml.safe_load_all(rendered))
        assert not any(
            resource.get("kind") in {"Deployment", "Service"}
            and resource.get("metadata", {}).get("name") in workload_names
            for resource in resources
        )

    templates = next(
        resource
        for resource in yaml.safe_load_all(
            subprocess.run(
                ["kubectl", "kustomize", str(INFERENCE_KUBERNETES_ROOT)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        if resource.get("kind") == "ConfigMap"
        and resource.get("metadata", {}).get("name") == "inference-workload-templates"
    )
    assert {"gpu-deployment.yaml", "cpu-deployment.yaml"} <= set(templates["data"])


def test_rendered_database_config_uses_shared_defaults_and_environment_overrides():
    for environment in ("dev", "staging", "prod"):
        resources = _render_environment(environment)
        database_config = next(
            resource
            for resource in resources
            if resource.get("kind") == "ConfigMap"
            and resource.get("metadata", {}).get("name")
            == "codefactory-database-config"
        )

        assert database_config["data"] == {
            "POSTGRES_ADMIN_USERNAME": "codefactory_admin",
            "GITHUB_DB_USERNAME": "codefactory_github",
            "GITHUB_DB_PORT": "5432",
            "GITHUB_DB_NAME": "codefactory",
            "GITHUB_DB_HOST": "codefactory-postgres",
            "GITHUB_DB_SSLMODE": "disable",
            "LANGGRAPH_DB_USERNAME": "codefactory_langgraph",
            "LANGGRAPH_DB_PORT": "5432",
            "LANGGRAPH_DB_NAME": "codefactory",
            "LANGGRAPH_DB_HOST": "codefactory-postgres",
            "LANGGRAPH_DB_SSLMODE": "disable",
            **yaml.safe_load(
                (
                    KUBERNETES_ROOT
                    / "environments"
                    / environment
                    / "database-config.yaml"
                ).read_text()
            )["data"],
        }


def test_service_deployments_source_usernames_and_passwords_separately():
    for service, username_key, password_key in (
        ("github-service", "GITHUB_DB_USERNAME", "GITHUB_DB_PASSWORD"),
        ("langgraph-service", "LANGGRAPH_DB_USERNAME", "LANGGRAPH_DB_PASSWORD"),
    ):
        deployment = (
            KUBERNETES_ROOT / "bases" / service / f"{service}.yaml"
        ).read_text()
        username_binding = rf"- name: {username_key}\s+valueFrom:\s+configMapKeyRef:\s+name: codefactory-database-config\s+key: {username_key}"
        password_binding = rf"- name: {password_key}\s+valueFrom:\s+secretKeyRef:\s+name: codefactory-runtime-secrets\s+key: {password_key}"
        assert re.search(username_binding, deployment)
        assert re.search(password_binding, deployment)


def test_runtime_secret_example_and_apply_script_exclude_usernames():
    secrets_directory = KUBERNETES_ROOT / "environments/dev/secrets"
    example_keys = {
        line.split("=", maxsplit=1)[0]
        for line in (secrets_directory / "runtime-secrets.env.example")
        .read_text()
        .splitlines()
        if line and not line.startswith("#")
    }
    expected_keys = {
        "POSTGRES_ADMIN_PASSWORD",
        "GITHUB_DB_PASSWORD",
        "LANGGRAPH_DB_PASSWORD",
        "GITHUB_WEBHOOK_SECRET",
        "INTERNAL_SERVICE_TOKEN",
        "MODEL_RECONCILER_TOKEN",
    }
    assert example_keys == expected_keys

    apply_script = (secrets_directory / "apply-secrets.sh").read_text()
    assert 'allowed["POSTGRES_ADMIN_PASSWORD"] = 1' in apply_script
    assert 'allowed["GITHUB_DB_USERNAME"]' not in apply_script
    assert 'allowed["LANGGRAPH_DB_USERNAME"]' not in apply_script
    assert "for key in POSTGRES_ADMIN_PASSWORD GITHUB_DB_PASSWORD " in apply_script


def test_postgres_and_migration_job_are_bundled_in_every_environment():
    for environment in ("dev", "staging", "prod"):
        resources = _render_environment(environment)
        postgres = next(
            resource
            for resource in resources
            if resource.get("kind") == "StatefulSet"
            and resource.get("metadata", {}).get("name") == "codefactory-postgres"
        )
        service = next(
            resource
            for resource in resources
            if resource.get("kind") == "Service"
            and resource.get("metadata", {}).get("name") == "codefactory-postgres"
        )
        job = next(
            resource
            for resource in resources
            if resource.get("kind") == "Job"
            and resource.get("metadata", {}).get("name")
            == "codefactory-database-migrations"
        )

        pod_spec = postgres["spec"]["template"]["spec"]
        assert postgres["spec"]["replicas"] == 1
        expected_claim_spec = {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "10Gi"}},
        }
        if environment == "dev":
            expected_claim_spec["storageClassName"] = "codefactory-ssd"
        assert (
            postgres["spec"]["volumeClaimTemplates"][0]["spec"] == expected_claim_spec
        )
        assert (
            service["spec"]["selector"] == postgres["spec"]["selector"]["matchLabels"]
        )
        assert pod_spec["automountServiceAccountToken"] is False

        database_container = next(
            container
            for container in pod_spec["containers"]
            if container["name"] == "postgres"
        )
        assert database_container["image"] == "postgres:16.10-bookworm"
        assert database_container["securityContext"]["runAsNonRoot"] is True
        secret_keys = {
            item["name"]: item["valueFrom"]["secretKeyRef"]["key"]
            for item in database_container["env"]
            if "secretKeyRef" in item.get("valueFrom", {})
        }
        assert secret_keys == {
            "POSTGRES_PASSWORD": "POSTGRES_ADMIN_PASSWORD",
            "PGPASSWORD": "POSTGRES_ADMIN_PASSWORD",
            "GITHUB_DB_PASSWORD": "GITHUB_DB_PASSWORD",
            "LANGGRAPH_DB_PASSWORD": "LANGGRAPH_DB_PASSWORD",
        }

        job_spec = job["spec"]["template"]["spec"]
        assert job_spec["automountServiceAccountToken"] is False
        assert job_spec["restartPolicy"] == "OnFailure"
        assert [container["name"] for container in job_spec["initContainers"]] == [
            "copy-github-migrations",
            "copy-langgraph-migration",
        ]
        assert (
            "003_github_task_installation.sql"
            in job_spec["initContainers"][0]["args"][0]
        )
        langgraph_migration_copy = job_spec["initContainers"][1]["args"][0]
        assert "013_global_model_downloads.sql" in langgraph_migration_copy
        assert "014_inference_model_apply.sql" in langgraph_migration_copy
        assert "015_inference_instance_lifecycle.sql" in langgraph_migration_copy
        assert "016_inference_workload_lifecycle.sql" in langgraph_migration_copy
        if environment == "dev":
            migration_profile = next(
                item["value"]
                for item in job_spec["containers"][0]["env"]
                if item["name"] == "MIGRATION_PROFILE"
            )
            assert migration_profile == "inference-015-016"
        assert job_spec["containers"][0]["command"] == [
            "python",
            "-m",
            "langgraph_service.storage.migrate",
        ]
        job_secrets = {
            item["name"]: item["valueFrom"]["secretKeyRef"]["key"]
            for item in job_spec["containers"][0]["env"]
            if "secretKeyRef" in item.get("valueFrom", {})
        }
        assert job_secrets == {
            "POSTGRES_ADMIN_PASSWORD": "POSTGRES_ADMIN_PASSWORD",
            "GITHUB_DB_PASSWORD": "GITHUB_DB_PASSWORD",
            "LANGGRAPH_DB_PASSWORD": "LANGGRAPH_DB_PASSWORD",
        }

        for deployment_name, schema_table in (
            ("github-service", "github_service.webhook_deliveries"),
            ("langgraph-service", "langgraph_service.checkpoint_migrations"),
        ):
            deployment = next(
                resource
                for resource in resources
                if resource.get("kind") == "Deployment"
                and resource.get("metadata", {}).get("name") == deployment_name
            )
            wait = deployment["spec"]["template"]["spec"]["initContainers"][0]
            assert wait["name"] == "wait-for-database-migrations"
            assert schema_table in wait["args"][0]
            assert "codefactory_migrations.migration_state" in wait["args"][0]

        settings = next(
            resource
            for resource in resources
            if resource.get("kind") == "ConfigMap"
            and resource.get("metadata", {}).get("name")
            == "codefactory-github-settings"
        )
        required_settings = {"GITHUB_APP_ID"}
        settings_data = settings["data"]
        assert isinstance(settings_data, dict)
        assert required_settings <= settings_data.keys()
        assert all(
            isinstance(settings_data[key], str) and settings_data[key].strip()
            for key in required_settings
        )
        github_deployment = next(
            resource
            for resource in resources
            if resource.get("kind") == "Deployment"
            and resource.get("metadata", {}).get("name") == "github-service"
        )
        github_env = github_deployment["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
        github_config_refs = {
            item["name"]: item["valueFrom"]["configMapKeyRef"]["key"]
            for item in github_env
            if "configMapKeyRef" in item.get("valueFrom", {})
            and item["valueFrom"]["configMapKeyRef"]["name"]
            == "codefactory-github-settings"
        }
        assert github_config_refs == {
            "GITHUB_APP_ID": "GITHUB_APP_ID",
        }
        assert not {
            "GITHUB_INSTALLATION_ID",
            "GITHUB_PROJECT_OWNER",
            "GITHUB_PROJECT_OWNER_TYPE",
            "GITHUB_PROJECT_NUMBER",
            "GITHUB_PROJECT_NODE_ID",
            "GITHUB_PROJECT_STATUS_FIELD_ID",
            "GITHUB_PROJECT_READY_OPTION_ID",
            "GITHUB_PROJECT_DONE_OPTION_ID",
        } & set(github_config_refs)


def test_dev_postgres_uses_retained_node_local_ssd_storage():
    dev_resources = _render_environment("dev")
    storage_class = next(
        resource
        for resource in dev_resources
        if resource.get("kind") == "StorageClass"
        and resource.get("metadata", {}).get("name") == "codefactory-ssd"
    )
    volume = next(
        resource
        for resource in dev_resources
        if resource.get("kind") == "PersistentVolume"
        and resource.get("metadata", {}).get("name") == "codefactory-postgres-data"
    )

    assert storage_class["provisioner"] == "kubernetes.io/no-provisioner"
    assert storage_class["reclaimPolicy"] == "Retain"
    assert storage_class["volumeBindingMode"] == "WaitForFirstConsumer"
    assert storage_class["metadata"] == {"name": "codefactory-ssd"}
    assert volume["spec"] == {
        "capacity": {"storage": "10Gi"},
        "volumeMode": "Filesystem",
        "accessModes": ["ReadWriteOnce"],
        "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": "codefactory-ssd",
        "local": {"path": "/mnt/k8s-data-ssd/codefactory/postgres"},
        "nodeAffinity": {
            "required": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": ["k8s-node01"],
                            }
                        ]
                    }
                ]
            }
        },
        "claimRef": {
            "namespace": "codefactory",
            "name": "postgres-data-codefactory-postgres-0",
        },
    }

    for environment in ("staging", "prod"):
        resources = _render_environment(environment)
        assert not any(resource.get("kind") == "StorageClass" for resource in resources)
        assert not any(
            resource.get("kind") == "PersistentVolume" for resource in resources
        )


def test_dev_deploy_preflight_validates_storage_and_guards_claim_recovery():
    deploy_script = (KUBERNETES_ROOT / "environments/dev/deploy.sh").read_text()

    for required_check in (
        'select(.kind == "StorageClass" and .metadata.name == "codefactory-ssd")',
        'select(.kind == "PersistentVolume" and .metadata.name == "codefactory-postgres-data")',
        "contract == ($expected | contract)",
        "specKeys: (.spec | keys)",
        "retained data will not be adopted or changed.",
        '.status.phase == "Pending"',
        '(.spec.volumeName // "") == ""',
        "preconditions:({uid:$uid,resourceVersion:$resource_version})",
        "resourceVersion:$resource_version",
        'if [[ "$recreate_pvc" == true ]]',
        'if [[ "$get_status" == 404 ]]',
    ):
        assert required_check in deploy_script


def test_dev_deploy_does_not_require_fixed_project_settings():
    deploy_script = (KUBERNETES_ROOT / "environments/dev/deploy.sh").read_text()

    for setting in (
        "GITHUB_PROJECT_OWNER",
        "GITHUB_PROJECT_OWNER_TYPE",
        "GITHUB_PROJECT_NUMBER",
    ):
        assert setting not in deploy_script


def test_installation_migration_is_additive_and_registered():
    migration = (
        REPOSITORY_ROOT
        / "codefactory/github-service/migrations/003_github_task_installation.sql"
    ).read_text()
    migration_runner = (
        REPOSITORY_ROOT
        / "codefactory/langgraph-service/src/langgraph_service/storage/migrate.py"
    ).read_text()

    assert "ADD COLUMN IF NOT EXISTS installation_id BIGINT" in migration
    assert "UPDATE github_service.task_contexts" not in migration
    assert '"003_github_task_installation.sql"' in migration_runner

    project_identity_migration = (
        REPOSITORY_ROOT
        / "codefactory/github-service/migrations/004_github_project_identity.sql"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS project_owner TEXT" in project_identity_migration
    assert (
        "ADD COLUMN IF NOT EXISTS project_owner_type TEXT" in project_identity_migration
    )
    assert '"004_github_project_identity.sql"' in migration_runner


def test_review_event_migration_extends_inbox_constraint_and_is_bundled():
    migration = (
        REPOSITORY_ROOT
        / "codefactory/github-service/migrations/005_github_review_event.sql"
    ).read_text()
    migration_runner = (
        REPOSITORY_ROOT
        / "codefactory/langgraph-service/src/langgraph_service/storage/migrate.py"
    ).read_text()
    migration_job = (
        KUBERNETES_ROOT / "bases/platform/database-migrations.yaml"
    ).read_text()

    assert "DROP CONSTRAINT IF EXISTS task_event_inbox_event_type_check" in migration
    assert "'review_changes_requested'" in migration
    assert '"005_github_review_event.sql"' in migration_runner
    assert (
        "cp /app/migrations/005_github_review_event.sql "
        "/migrations/005_github_review_event.sql"
    ) in migration_job


def test_agent_roster_migration_is_registered_and_bundled():
    migration = (
        REPOSITORY_ROOT
        / "codefactory/langgraph-service/migrations/003_agent_roster.sql"
    ).read_text()
    migration_runner = (
        REPOSITORY_ROOT
        / "codefactory/langgraph-service/src/langgraph_service/storage/migrate.py"
    ).read_text()
    migration_job = (
        KUBERNETES_ROOT / "bases/platform/database-migrations.yaml"
    ).read_text()

    assert "task_agent_roster_snapshot" in migration
    assert '"003_agent_roster.sql"' in migration_runner
    assert (
        "cp /app/migrations/003_agent_roster.sql /migrations/003_agent_roster.sql"
    ) in migration_job
