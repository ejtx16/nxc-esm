Feature: Audit trail for every agent action and reasoning step (demo item D7)

  Scenario: The LicenseCore outage is fully traceable
    Given the LicenseCore outage scenario has created a P1 incident
    Then every processed event has an audit trace
    And the P1 triage trace records its steps, tool calls and latency
    And alert classification accuracy is recorded against ground truth
    And the audit metrics cover iteration, tools, tokens, accuracy, activity and latency
