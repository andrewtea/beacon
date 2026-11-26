"""The mock scenario: an invented San Francisco fire, told as each source's answers.

    from beacon.scenario import Scenario, scenario_transport

    scenario = Scenario(start=clock.now())
    http = SourceHttp(scenario_transport(scenario, clock.now))

See `fire` for the story and `transport` for how it stands in for the network.
"""

from beacon.scenario.fire import DURATION, FIRE, INVENTED, Scenario
from beacon.scenario.transport import scenario_transport

__all__ = ["DURATION", "FIRE", "INVENTED", "Scenario", "scenario_transport"]
