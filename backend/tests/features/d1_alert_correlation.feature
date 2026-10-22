Feature: Real-time alert ingestion and correlation (demo item D1)
  Duplicate and related alerts collapse into one incident that is acknowledged without waiting for a human.

  Scenario: 47 duplicate LicenseCore alerts collapse into one acknowledged incident
    Given the seeded NexaCore database
    When 47 duplicate "Service health check failed" Critical alerts arrive from "LicenseCore" over 22 minutes
    Then exactly 1 alert cluster exists
    And that cluster contains 47 alerts
    And exactly 1 incident is open for that cluster
    And the cluster was acknowledged within 180 seconds of its first alert

  Scenario: Related BillingEngine alerts join the same incident
    Given the seeded NexaCore database
    When 10 duplicate "Service health check failed" Critical alerts arrive from "LicenseCore" over 5 minutes
    And 3 duplicate "Queue depth > 10,000" High alerts arrive from "BillingEngine" over 5 minutes
    Then exactly 1 alert cluster exists
    And exactly 1 incident is open for that cluster

  Scenario: Self-recovering low-severity noise is suppressed and audited
    Given the seeded NexaCore database
    When a self-recovering Low alert "Cache hit rate dropped below 40%" arrives from "ReportingPlatform"
    Then the cluster is suppressed as a false positive with confidence of at least 0.85
    And no incident is opened
    And the audit log records the suppression verdict

  Scenario: A Critical alert is never suppressed
    Given the seeded NexaCore database
    When a self-recovering Critical alert "Memory usage spike" arrives from "NotificationService"
    Then the cluster is not suppressed
