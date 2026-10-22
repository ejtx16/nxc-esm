Feature: Before/after KPI simulation on the provided datasets (demo item D6)

  Scenario: Every KPI is reported against its case-study baseline and labelled
    Given the KPI simulation has run on the provided datasets
    Then 10 KPIs are reported with their baselines
    And each KPI is labelled "measured" or "modelled"

  Scenario: Measured KPIs improve on the baseline
    Given the KPI simulation has run on the provided datasets
    Then the alert noise ratio is below its baseline of 53 percent
    And the critical alert ack time is under 3 minutes
    And the average triage time is under 2 minutes
    And the self-service rate is above 40 percent

  Scenario: The scenario generator reproduces the case's raw noise level
    Given the KPI simulation has run on the provided datasets
    Then raw alert noise in the scenarios is within 5 points of 53 percent
