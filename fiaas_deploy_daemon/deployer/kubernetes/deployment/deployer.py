#!/usr/bin/env python
# -*- coding: utf-8
import logging
import shlex

from k8s.client import NotFound
from k8s.models.common import ObjectMeta
from k8s.models.deployment import Deployment, DeploymentSpec, PodTemplateSpec, LabelSelector, DeploymentStrategy, \
    RollingUpdateDeployment
from k8s.models.pod import ContainerPort, EnvVar, HTTPGetAction, TCPSocketAction, ExecAction, HTTPHeader, Container, \
    PodSpec, VolumeMount, Volume, ResourceRequirements, Probe, ConfigMapEnvSource, \
    ConfigMapVolumeSource, EmptyDirVolumeSource, EnvFromSource, EnvVarSource, Lifecycle, Handler, \
    ResourceFieldSelector, ObjectFieldSelector

from fiaas_deploy_daemon.deployer.kubernetes.autoscaler import should_have_autoscaler
from fiaas_deploy_daemon.retry import retry_on_upsert_conflict
from fiaas_deploy_daemon.tools import merge_dicts

LOG = logging.getLogger(__name__)


class DeploymentDeployer(object):
    MINIMUM_GRACE_PERIOD = 30

    def __init__(self, config, datadog, prometheus, deployment_secrets):
        self._datadog = datadog
        self._prometheus = prometheus
        self._secrets = deployment_secrets
        self._fiaas_env = _build_fiaas_env(config)
        self._global_env = config.global_env
        self._lifecycle = None
        self._grace_period = self.MINIMUM_GRACE_PERIOD
        self._use_in_memory_emptydirs = config.use_in_memory_emptydirs
        if config.pre_stop_delay > 0:
            self._lifecycle = Lifecycle(preStop=Handler(
                _exec=ExecAction(command=["sleep", str(config.pre_stop_delay)])))
            self._grace_period += config.pre_stop_delay
        self._max_surge = config.deployment_max_surge
        self._max_unavailable = config.deployment_max_unavailable

    @retry_on_upsert_conflict(max_value_seconds=5, max_tries=5)
    def deploy(self, app_spec, selector, labels, besteffort_qos_is_required):
        LOG.info("Creating new deployment for %s", app_spec.name)
        deployment_labels = merge_dicts(app_spec.labels.deployment, labels)
        metadata = ObjectMeta(name=app_spec.name, namespace=app_spec.namespace, labels=deployment_labels,
                              annotations=app_spec.annotations.deployment)
        container_ports = [ContainerPort(name=port_spec.name, containerPort=port_spec.target_port) for port_spec in
                           app_spec.ports]
        env = self._make_env(app_spec)
        pull_policy = "IfNotPresent" if (":" in app_spec.image and ":latest" not in app_spec.image) else "Always"

        env_from = [EnvFromSource(configMapRef=ConfigMapEnvSource(name=app_spec.name, optional=True))]
        containers = [
            Container(name=app_spec.name,
                      image=app_spec.image,
                      ports=container_ports,
                      env=env,
                      envFrom=env_from,
                      lifecycle=self._lifecycle,
                      livenessProbe=_make_probe(app_spec.health_checks.liveness),
                      readinessProbe=_make_probe(app_spec.health_checks.readiness),
                      imagePullPolicy=pull_policy,
                      volumeMounts=self._make_volume_mounts(app_spec),
                      resources=_make_resource_requirements(app_spec.resources))
        ]

        automount_service_account_token = app_spec.admin_access
        init_containers = []
        service_account_name = "default"

        pod_spec = PodSpec(containers=containers,
                           initContainers=init_containers,
                           volumes=self._make_volumes(app_spec),
                           serviceAccountName=service_account_name,
                           automountServiceAccountToken=automount_service_account_token,
                           terminationGracePeriodSeconds=self._grace_period)

        pod_labels = merge_dicts(app_spec.labels.pod, _add_status_label(labels))
        pod_metadata = ObjectMeta(name=app_spec.name, namespace=app_spec.namespace, labels=pod_labels,
                                  annotations=app_spec.annotations.pod)
        pod_template_spec = PodTemplateSpec(metadata=pod_metadata, spec=pod_spec)
        replicas = app_spec.replicas
        # we must avoid that the deployment scales up to app_spec.replicas if autoscaler has set another value
        if should_have_autoscaler(app_spec):
            try:
                deployment = Deployment.get(app_spec.name, app_spec.namespace)
                replicas = deployment.spec.replicas
                LOG.info("Configured replica size (%d) for deployment is being ignored, as current running replica size"
                         " is different (%d) for %s", app_spec.replicas, deployment.spec.replicas, app_spec.name)
            except NotFound:
                pass

        deployment_strategy = DeploymentStrategy(
            rollingUpdate=RollingUpdateDeployment(maxUnavailable=self._max_unavailable,
                                                  maxSurge=self._max_surge))
        if app_spec.replicas == 1 and app_spec.singleton:
            deployment_strategy = DeploymentStrategy(
                rollingUpdate=RollingUpdateDeployment(maxUnavailable=1, maxSurge=0))
        spec = DeploymentSpec(replicas=replicas, selector=LabelSelector(matchLabels=selector),
                              template=pod_template_spec, revisionHistoryLimit=5,
                              strategy=deployment_strategy)

        deployment = Deployment.get_or_create(metadata=metadata, spec=spec)
        _clear_pod_init_container_annotations(deployment)
        self._datadog.apply(deployment, app_spec, besteffort_qos_is_required)
        self._prometheus.apply(deployment, app_spec)
        self._secrets.apply(deployment, app_spec)
        deployment.save()

    def delete(self, app_spec):
        LOG.info("Deleting deployment for %s", app_spec.name)
        try:
            body = {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Foreground"}
            Deployment.delete(app_spec.name, app_spec.namespace, body=body)
        except NotFound:
            pass

    def _make_volumes(self, app_spec):
        volumes = []
        volumes.append(Volume(name="{}-config".format(app_spec.name),
                              configMap=ConfigMapVolumeSource(name=app_spec.name, optional=True)))
        if self._use_in_memory_emptydirs:
            empty_dir_volume_source = EmptyDirVolumeSource(medium="Memory")
        else:
            empty_dir_volume_source = EmptyDirVolumeSource()
        volumes.append(Volume(name="tmp", emptyDir=empty_dir_volume_source))
        return volumes

    def _make_volume_mounts(self, app_spec):
        volume_mounts = []
        volume_mounts.append(
            VolumeMount(name="{}-config".format(app_spec.name), readOnly=True, mountPath="/var/run/config/fiaas/"))
        volume_mounts.append(VolumeMount(name="tmp", readOnly=False, mountPath="/tmp"))
        return volume_mounts

    def _make_env(self, app_spec):
        constants = self._fiaas_env.copy()
        constants["ARTIFACT_NAME"] = app_spec.name
        constants["IMAGE"] = app_spec.image
        constants["VERSION"] = app_spec.version
        env = [EnvVar(name=name, value=value) for name, value in constants.iteritems()]

        # For backward compatibility. https://github.schibsted.io/finn/fiaas-deploy-daemon/pull/34
        global_env = []
        for name, value in self._global_env.iteritems():
            if "FIAAS_{}".format(name) not in constants and name not in constants:
                global_env.extend([EnvVar(name=name, value=value), EnvVar(name="FIAAS_{}".format(name), value=value)])
            else:
                LOG.warn("Reserved environment-variable: {} declared as global. Ignoring and continuing".format(name))
        env.extend(global_env)

        env.extend([
            EnvVar(name="FIAAS_REQUESTS_CPU", valueFrom=EnvVarSource(
                resourceFieldRef=ResourceFieldSelector(containerName=app_spec.name, resource="requests.cpu",
                                                       divisor=1))),
            EnvVar(name="FIAAS_REQUESTS_MEMORY", valueFrom=EnvVarSource(
                resourceFieldRef=ResourceFieldSelector(containerName=app_spec.name, resource="requests.memory",
                                                       divisor=1))),
            EnvVar(name="FIAAS_LIMITS_CPU", valueFrom=EnvVarSource(
                resourceFieldRef=ResourceFieldSelector(containerName=app_spec.name, resource="limits.cpu", divisor=1))),
            EnvVar(name="FIAAS_LIMITS_MEMORY", valueFrom=EnvVarSource(
                resourceFieldRef=ResourceFieldSelector(containerName=app_spec.name, resource="limits.memory",
                                                       divisor=1))),
            EnvVar(name="FIAAS_NAMESPACE", valueFrom=EnvVarSource(
                fieldRef=ObjectFieldSelector(fieldPath="metadata.namespace"))),
            EnvVar(name="FIAAS_POD_NAME", valueFrom=EnvVarSource(
                fieldRef=ObjectFieldSelector(fieldPath="metadata.name"))),
        ])
        env.sort(key=lambda x: x.name)
        return env


def _clear_pod_init_container_annotations(deployment):
    """Kubernetes 1.5 implemented init-containers using annotations, and in order to preserve backwards compatibility in
    1.6 and 1.7, those annotations take precedence over the actual initContainer element in the spec object. In order to
    ensure that any changes we make take effect, we clear the annotations.
    """
    keys_to_clear = set()
    try:
        if deployment.spec.template.metadata.annotations:
            for key, _ in deployment.spec.template.metadata.annotations.items():
                if key.endswith("kubernetes.io/init-containers"):
                    keys_to_clear.add(key)
            for key in keys_to_clear:
                del deployment.spec.template.metadata.annotations[key]
    except AttributeError:
        pass


def _add_status_label(labels):
    copy = labels.copy()
    copy.update({
        "fiaas/status": "active"
    })
    return copy


def _make_resource_requirements(resources_spec):
    def as_dict(resource_requirement_spec):
        return {"cpu": resource_requirement_spec.cpu, "memory": resource_requirement_spec.memory}

    return ResourceRequirements(limits=as_dict(resources_spec.limits), requests=as_dict(resources_spec.requests))


def _make_probe(check_spec):
    probe = Probe(initialDelaySeconds=check_spec.initial_delay_seconds,
                  timeoutSeconds=check_spec.timeout_seconds,
                  successThreshold=check_spec.success_threshold,
                  failureThreshold=check_spec.failure_threshold,
                  periodSeconds=check_spec.period_seconds)
    if check_spec.http:
        probe.httpGet = HTTPGetAction(path=check_spec.http.path, port=check_spec.http.port,
                                      httpHeaders=[HTTPHeader(name=name, value=value)
                                                   for name, value in check_spec.http.http_headers.items()])
    elif check_spec.tcp:
        probe.tcpSocket = TCPSocketAction(port=check_spec.tcp.port)
    elif check_spec.execute:
        probe._exec = ExecAction(command=shlex.split(check_spec.execute.command))
    else:
        raise RuntimeError("AppSpec must have exactly one health check, none was defined.")

    return probe


def _build_fiaas_env(config):
    env = {
        "FIAAS_INFRASTRUCTURE": config.infrastructure,  # DEPRECATED. Remove in the future.
        "LOG_STDOUT": "true",
        "LOG_FORMAT": config.log_format,
        "CONSTRETTO_TAGS": "kubernetes",  # DEPRECATED. Remove in the future.
    }
    if config.environment:
        env.update({
            "FINN_ENV": config.environment,  # DEPRECATED. Remove in the future.
            "FIAAS_ENVIRONMENT": config.environment,
            "CONSTRETTO_TAGS": ",".join(("kubernetes-{}".format(config.environment), "kubernetes", config.environment)),
        })
    return env
