from datadog_checks.checks import AgentCheck
from datadog_checks.utils.subprocess_output import get_subprocess_output
from datadog_checks.base.errors import CheckException

__version__ = "1.0.0"

class ServiceCheck(AgentCheck):

    def check(self, instance):

        health = instance.get('health_status', 'healthy')
        monitored_services = instance.get('container_names', [])
        if monitored_services is None or monitored_services == []:
          return

        running_containers = self.get_running(health)

        for name in monitored_services:

          status = AgentCheck.CRITICAL
          if name in running_containers:
            status = AgentCheck.OK

          self.service_check("service.up", status, tags=["service:{}".format(name)])

    def get_running(self, health):

        out, err, retcode = get_subprocess_output(["docker", "ps", "--filter", "health={}".format(health), "--filter", "status=running", "--format", "{{.Names}}"], self.log, raise_on_empty_output=False)
        if retcode != 0:
          raise CheckException("Error while running docker ps: {}".format(err))
        if out == "":
          return []

        return out.split("\n")
