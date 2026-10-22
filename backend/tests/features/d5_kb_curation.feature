Feature: KB accuracy score and update recommendation (demo item D5)

  Scenario: Every article is scored and given a recommendation
    Given the seeded NexaCore database
    Then all 30 KB articles have an accuracy score and a keep, update or archive recommendation

  Scenario: A stale LicenseCore article is flagged and a fix is drafted for approval
    Given the LicenseCore outage scenario has created a P1 incident
    Then article "KB4022" scores below the 90 percent threshold
    And a draft update for "KB4022" is waiting for approval
    And the draft removes the deprecated references

  Scenario: Approving the drafted update raises the score above the threshold
    Given the LicenseCore outage scenario has created a P1 incident
    When the knowledge manager approves the draft for "KB4022"
    Then article "KB4022" scores at least 90 percent
    And triage may now recommend article "KB4022"
