Feature: Automated incident triage with explanation (demo item D2)

  Scenario: A Critical Tier-0 alert is triaged as a P1 with an explanation
    Given the LicenseCore outage scenario has created a P1 incident
    Then the incident is classified as P1 and routed to "App Support L2"
    And the triage rationale mentions "Tier 0"
    And the incident was escalated immediately

  Scenario: KB articles below the accuracy threshold are not attached
    Given the LicenseCore outage scenario has created a P1 incident
    Then no KB article below 90 percent accuracy is attached
    And the rationale explains which articles were excluded

  Scenario: An unresolved P2 is escalated at half of its SLA window
    Given an assigned P2 incident with an 8 hour SLA
    When 4.5 hours pass
    Then the incident is escalated before the SLA is breached

  Scenario: Triage is scored against the hand-labelled golden set
    Given the golden triage labels
    When the triage agent classifies each golden incident
    Then category accuracy is at least 80 percent
    And priority accuracy within one level is at least 90 percent
