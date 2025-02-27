#!/usr/bin/env python
# -*- coding: utf-8

import os
import subprocess
import sys
import time

import pytest
import requests
from k8s import config
from k8s.client import NotFound, Client
from k8s.models.autoscaler import HorizontalPodAutoscaler
from k8s.models.common import ObjectMeta
from k8s.models.deployment import Deployment
from k8s.models.ingress import Ingress
from k8s.models.service import Service

from fiaas_deploy_daemon.crd.types import FiaasApplication, FiaasApplicationStatus, FiaasApplicationSpec
from fiaas_deploy_daemon.tools import merge_dicts
from fiaas_deploy_daemon.tpr.status import create_name
from fiaas_deploy_daemon.tpr.types import PaasbetaApplication, PaasbetaApplicationSpec, PaasbetaStatus
from minikube import MinikubeError
from utils import wait_until, tpr_available, crd_available, tpr_supported, crd_supported, skip_if_tpr_not_supported, \
    skip_if_crd_not_supported, read_yml, sanitize_resource_name, assert_k8s_resource_matches, get_unbound_port

IMAGE1 = u"finntech/application-name:123"
IMAGE2 = u"finntech/application-name:321"
DEPLOYMENT_ID1 = u"deployment_id_1"
DEPLOYMENT_ID2 = u"deployment_id_2"
PATIENCE = 30
TIMEOUT = 5


def _fixture_names(fixture_value):
    name, data = fixture_value
    return name


@pytest.mark.integration_test
class TestE2E(object):
    @pytest.fixture(scope="module", params=("ClusterIP", "NodePort"))
    def service_type(self, request):
        return request.param

    @pytest.fixture(scope="module")
    def kubernetes(self, minikube_installer, service_type, k8s_version):
        try:
            minikube = minikube_installer.new(profile=service_type, k8s_version=k8s_version)
            try:
                minikube.start()
                yield {
                    "server": minikube.server,
                    "client-cert": minikube.client_cert,
                    "client-key": minikube.client_key,
                    "api-cert": minikube.api_cert
                }
            finally:
                minikube.delete()
        except MinikubeError as e:
            msg = "Unable to run minikube: %s"
            pytest.fail(msg % str(e))

    @pytest.fixture(autouse=True)
    def k8s_client(self, kubernetes):
        Client.clear_session()
        config.api_server = kubernetes["server"]
        config.debug = True
        config.verify_ssl = False
        config.cert = (kubernetes["client-cert"], kubernetes["client-key"])

    @pytest.fixture(scope="module")
    def fdd(self, kubernetes, service_type, k8s_version, use_docker_for_e2e):
        port = get_unbound_port()
        args = [
            "fiaas-deploy-daemon",
            "--port", str(port),
            "--api-server", kubernetes["server"],
            "--api-cert", kubernetes["api-cert"],
            "--client-cert", kubernetes["client-cert"],
            "--client-key", kubernetes["client-key"],
            "--service-type", service_type,
            "--ingress-suffix", "svc.test.example.com",
            "--environment", "test",
            "--datadog-container-image", "DATADOG_IMAGE",
            "--strongbox-init-container-image", "STRONGBOX_IMAGE",
            "--use-ingress-tls", "default_off",
        ]
        if tpr_supported(k8s_version):
            args.append("--enable-tpr-support")
        if crd_supported(k8s_version):
            args.append("--enable-crd-support")
        cert_path = os.path.dirname(kubernetes["api-cert"])
        args = use_docker_for_e2e(cert_path, service_type, k8s_version, port) + args
        fdd = subprocess.Popen(args, stdout=sys.stderr, env=merge_dicts(os.environ, {"NAMESPACE": "default"}))
        time.sleep(1)
        if fdd.poll() is not None:
            pytest.fail("fiaas-deploy-daemon has crashed after startup, inspect logs")

        def ready():
            resp = requests.get("http://localhost:{}/healthz".format(port), timeout=TIMEOUT)
            resp.raise_for_status()

        try:
            wait_until(ready, "web-interface healthy", RuntimeError, patience=PATIENCE)
            if tpr_supported(k8s_version):
                wait_until(tpr_available(kubernetes, timeout=TIMEOUT), "TPR available", RuntimeError, patience=PATIENCE)
            if crd_supported(k8s_version):
                wait_until(crd_available(kubernetes, timeout=TIMEOUT), "CRD available", RuntimeError, patience=PATIENCE)
            yield "http://localhost:{}/fiaas".format(port)
        finally:
            self._end_popen(fdd)

    @pytest.fixture(ids=_fixture_names, params=(
            ("data/v2minimal.yml", {
                Service: "e2e_expected/v2minimal-service.yml",
                Deployment: "e2e_expected/v2minimal-deployment.yml",
                Ingress: "e2e_expected/v2minimal-ingress.yml",
            }),
            ("v2/data/examples/host.yml", {
                Service: "e2e_expected/host-service.yml",
                Deployment: "e2e_expected/host-deployment.yml",
                Ingress: "e2e_expected/host-ingress.yml",
            }),
            ("v2/data/examples/exec_config.yml", {
                Service: "e2e_expected/exec-service.yml",
                Deployment: "e2e_expected/exec-deployment.yml",
                Ingress: "e2e_expected/exec-ingress.yml",
            }),
            ("v2/data/examples/tcp_ports.yml", {
                Service: "e2e_expected/tcp_ports-service.yml",
                Deployment: "e2e_expected/tcp_ports-deployment.yml",
            }),
            ("v2/data/examples/single_tcp_port.yml", {
                Service: "e2e_expected/single_tcp_port-service.yml",
                Deployment: "e2e_expected/single_tcp_port-deployment.yml",
            }),
            ("v2/data/examples/partial_override.yml", {
                Service: "e2e_expected/partial_override-service.yml",
                Deployment: "e2e_expected/partial_override-deployment.yml",
                Ingress: "e2e_expected/partial_override-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/partial_override-hpa.yml",
            }),
            ("v3/data/examples/v3minimal.yml", {
                Service: "e2e_expected/v3minimal-service.yml",
                Deployment: "e2e_expected/v3minimal-deployment.yml",
                Ingress: "e2e_expected/v3minimal-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/v3minimal-hpa.yml",
            }),
            ("v3/data/examples/full.yml", {
                Service: "e2e_expected/v3full-service.yml",
                Deployment: "e2e_expected/v3full-deployment.yml",
                Ingress: "e2e_expected/v3full-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/v3full-hpa.yml",
            }),
            ("v3/data/examples/multiple_hosts_multiple_paths.yml", {
                Service: "e2e_expected/multiple_hosts_multiple_paths-service.yml",
                Deployment: "e2e_expected/multiple_hosts_multiple_paths-deployment.yml",
                Ingress: "e2e_expected/multiple_hosts_multiple_paths-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/multiple_hosts_multiple_paths-hpa.yml",
            }),
            ("v3/data/examples/strongbox.yml", {
                Service: "e2e_expected/strongbox-service.yml",
                Deployment: "e2e_expected/strongbox-deployment.yml",
                Ingress: "e2e_expected/strongbox-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/strongbox-hpa.yml",
            }),
            ("v3/data/examples/tls_enabled.yml", {
                Service: "e2e_expected/tls-service.yml",
                Deployment: "e2e_expected/tls-deployment.yml",
                Ingress: "e2e_expected/tls-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/tls-hpa.yml",
            }),
            ("v3/data/examples/tls_enabled_cert_issuer.yml", {
                Service: "e2e_expected/tls-service-cert-issuer.yml",
                Deployment: "e2e_expected/tls-deployment-cert-issuer.yml",
                Ingress: "e2e_expected/tls-ingress-cert-issuer.yml",
                HorizontalPodAutoscaler: "e2e_expected/tls-hpa-cert-issuer.yml",
            }),
    ))
    def third_party_resource(self, request, k8s_version):
        fiaas_path, expected = request.param
        skip_if_tpr_not_supported(k8s_version)

        fiaas_yml = read_yml(request.fspath.dirpath().join("specs").join(fiaas_path).strpath)
        expected = {kind: read_yml(request.fspath.dirpath().join(path).strpath) for kind, path in expected.items()}

        name = sanitize_resource_name(fiaas_path)
        metadata = ObjectMeta(name=name, namespace="default", labels={"fiaas/deployment_id": DEPLOYMENT_ID1})
        spec = PaasbetaApplicationSpec(application=name, image=IMAGE1, config=fiaas_yml)
        request.addfinalizer(lambda: self._ensure_clean(name, expected))
        return name, PaasbetaApplication(metadata=metadata, spec=spec), expected

    @pytest.fixture(ids=_fixture_names, params=(
            ("data/v2minimal.yml", {
                Service: "e2e_expected/v2minimal-service.yml",
                Deployment: "e2e_expected/v2minimal-deployment.yml",
                Ingress: "e2e_expected/v2minimal-ingress.yml",
            }),
            ("v2/data/examples/host.yml", {
                Service: "e2e_expected/host-service.yml",
                Deployment: "e2e_expected/host-deployment.yml",
                Ingress: "e2e_expected/host-ingress.yml",
            }),
            ("v2/data/examples/exec_config.yml", {
                Service: "e2e_expected/exec-service.yml",
                Deployment: "e2e_expected/exec-deployment.yml",
                Ingress: "e2e_expected/exec-ingress.yml",
            }),
            ("v2/data/examples/tcp_ports.yml", {
                Service: "e2e_expected/tcp_ports-service.yml",
                Deployment: "e2e_expected/tcp_ports-deployment.yml",
            }),
            ("v2/data/examples/single_tcp_port.yml", {
                Service: "e2e_expected/single_tcp_port-service.yml",
                Deployment: "e2e_expected/single_tcp_port-deployment.yml",
            }),
            ("v2/data/examples/partial_override.yml", {
                Service: "e2e_expected/partial_override-service.yml",
                Deployment: "e2e_expected/partial_override-deployment.yml",
                Ingress: "e2e_expected/partial_override-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/partial_override-hpa.yml",
            }),
            ("v3/data/examples/v3minimal.yml", {
                Service: "e2e_expected/v3minimal-service.yml",
                Deployment: "e2e_expected/v3minimal-deployment.yml",
                Ingress: "e2e_expected/v3minimal-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/v3minimal-hpa.yml",
            }),
            ("v3/data/examples/full.yml", {
                Service: "e2e_expected/v3full-service.yml",
                Deployment: "e2e_expected/v3full-deployment.yml",
                Ingress: "e2e_expected/v3full-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/v3full-hpa.yml",
            }),
            ("v3/data/examples/multiple_hosts_multiple_paths.yml", {
                Service: "e2e_expected/multiple_hosts_multiple_paths-service.yml",
                Deployment: "e2e_expected/multiple_hosts_multiple_paths-deployment.yml",
                Ingress: "e2e_expected/multiple_hosts_multiple_paths-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/multiple_hosts_multiple_paths-hpa.yml",
            }),
            ("v3/data/examples/strongbox.yml", {
                Service: "e2e_expected/strongbox-service.yml",
                Deployment: "e2e_expected/strongbox-deployment.yml",
                Ingress: "e2e_expected/strongbox-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/strongbox-hpa.yml",
            }),
            ("v3/data/examples/single-replica-singleton.yml", {
                Deployment: "e2e_expected/single-replica-singleton.yml",
            }),
            ("v3/data/examples/single-replica-not-singleton.yml", {
                Deployment: "e2e_expected/single-replica-not-singleton.yml",
            }),
            ("v3/data/examples/tls_enabled.yml", {
                Service: "e2e_expected/tls-service.yml",
                Deployment: "e2e_expected/tls-deployment.yml",
                Ingress: "e2e_expected/tls-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/tls-hpa.yml",
            }),
            ("v3/data/examples/tls_enabled_cert_issuer.yml", {
                Service: "e2e_expected/tls-service-cert-issuer.yml",
                Deployment: "e2e_expected/tls-deployment-cert-issuer.yml",
                Ingress: "e2e_expected/tls-ingress-cert-issuer.yml",
                HorizontalPodAutoscaler: "e2e_expected/tls-hpa-cert-issuer.yml",
            }),
    ))
    def custom_resource_definition(self, request, k8s_version):
        fiaas_path, expected = request.param

        skip_if_crd_not_supported(k8s_version)
        fiaas_yml = read_yml(request.fspath.dirpath().join("specs").join(fiaas_path).strpath)
        expected = {kind: read_yml(request.fspath.dirpath().join(path).strpath) for kind, path in expected.items()}

        name = sanitize_resource_name(fiaas_path)
        metadata = ObjectMeta(name=name, namespace="default", labels={"fiaas/deployment_id": DEPLOYMENT_ID1})
        spec = FiaasApplicationSpec(application=name, image=IMAGE1, config=fiaas_yml)
        request.addfinalizer(lambda: self._ensure_clean(name, expected))
        return name, FiaasApplication(metadata=metadata, spec=spec), expected

    def _ensure_clean(self, name, expected):
        kinds = self._select_kinds(expected)
        for kind in kinds:
            try:
                kind.delete(name)
            except NotFound:
                pass

    @staticmethod
    def _end_popen(popen):
        popen.terminate()
        time.sleep(1)
        if popen.poll() is None:
            popen.kill()

    @staticmethod
    def _select_kinds(expected):
        if len(expected.keys()) > 0:
            return expected.keys()
        else:
            return [Service, Deployment, Ingress]

    @pytest.mark.usefixtures("fdd")
    def test_third_party_resource_deploy(self, third_party_resource, service_type):
        name, paasbetaapplication, expected = third_party_resource

        # check that k8s objects for name doesn't already exist
        kinds = self._select_kinds(expected)
        for kind in kinds:
            with pytest.raises(NotFound):
                kind.get(name)

        # First deploy
        paasbetaapplication.save()

        # Check that deployment status is RUNNING
        def _assert_status():
            status = PaasbetaStatus.get(create_name(name, DEPLOYMENT_ID1))
            assert status.result == u"RUNNING"
            assert len(status.logs) > 0
            assert any("Saving result RUNNING for default/{}".format(name) in l for l in status.logs)

        wait_until(_assert_status, patience=PATIENCE)

        # Check deploy success
        wait_until(_deploy_success(name, kinds, service_type, IMAGE1, expected, DEPLOYMENT_ID1), patience=PATIENCE)

        # Redeploy, new image, possibly new init-container
        paasbetaapplication.spec.image = IMAGE2
        paasbetaapplication.metadata.labels["fiaas/deployment_id"] = DEPLOYMENT_ID2
        strongbox_groups = []
        if "strongbox" in name:
            strongbox_groups = ["foo", "bar"]
            paasbetaapplication.spec.config["extensions"]["strongbox"]["groups"] = strongbox_groups
        paasbetaapplication.save()

        # Check success
        wait_until(_deploy_success(name, kinds, service_type, IMAGE2, expected, DEPLOYMENT_ID2, strongbox_groups),
                   patience=PATIENCE)

        # Cleanup
        PaasbetaApplication.delete(name)

        def cleanup_complete():
            for kind in kinds:
                with pytest.raises(NotFound):
                    kind.get(name)

        wait_until(cleanup_complete, patience=PATIENCE)

    @pytest.mark.usefixtures("fdd")
    def test_custom_resource_definition_deploy(self, custom_resource_definition, service_type):
        name, fiaas_application, expected = custom_resource_definition

        # check that k8s objects for name doesn't already exist
        kinds = self._select_kinds(expected)
        for kind in kinds:
            with pytest.raises(NotFound):
                kind.get(name)

        # First deploy
        fiaas_application.save()

        # Check that deployment status is RUNNING
        def _assert_status():
            status = FiaasApplicationStatus.get(create_name(name, DEPLOYMENT_ID1))
            assert status.result == u"RUNNING"
            assert len(status.logs) > 0
            assert any("Saving result RUNNING for default/{}".format(name) in l for l in status.logs)

        wait_until(_assert_status, patience=PATIENCE)

        # Check deploy success
        wait_until(_deploy_success(name, kinds, service_type, IMAGE1, expected, DEPLOYMENT_ID1), patience=PATIENCE)

        # Redeploy, new image, possibly new init-container
        fiaas_application.spec.image = IMAGE2
        fiaas_application.metadata.labels["fiaas/deployment_id"] = DEPLOYMENT_ID2
        strongbox_groups = []
        if "strongbox" in name:
            strongbox_groups = ["foo", "bar"]
            fiaas_application.spec.config["extensions"]["strongbox"]["groups"] = strongbox_groups
        fiaas_application.save()

        # Check success
        wait_until(_deploy_success(name, kinds, service_type, IMAGE2, expected, DEPLOYMENT_ID2, strongbox_groups),
                   patience=PATIENCE)

        # Cleanup
        FiaasApplication.delete(name)

        def cleanup_complete():
            for kind in kinds:
                with pytest.raises(NotFound):
                    kind.get(name)

        wait_until(cleanup_complete, patience=PATIENCE)


def _deploy_success(name, kinds, service_type, image, expected, deployment_id, strongbox_groups=None):
    def action():
        for kind in kinds:
            assert kind.get(name)
        dep = Deployment.get(name)
        assert dep.spec.template.spec.containers[0].image == image
        svc = Service.get(name)
        assert svc.spec.type == service_type

        for kind, expected_dict in expected.items():
            actual = kind.get(name)
            assert_k8s_resource_matches(actual, expected_dict, image, service_type, deployment_id, strongbox_groups)

    return action
