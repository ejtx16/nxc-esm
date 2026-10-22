Feature: Self-service SR fulfilment end to end (demo item D4)

  Scenario: A password reset completes end to end
    Given the seeded demo database
    When employee "EMP1002" submits a self-service "Password Reset"
    Then the request is completed by the agent
    And a temporary credential was issued through the mock IAM
    And the fulfilment trace shows identity verification

  Scenario: Admin access is routed to a human approver
    Given the seeded demo database
    When employee "EMP1003" submits a self-service "Access Provisioning" for "BillingEngine" at "admin" level
    Then the request is waiting for approval
    And no access was granted

  Scenario: Two portal requests take the low-risk and high-risk paths
    Given the seeded demo database
    When the "self_service_requests" scenario is replayed
    Then the password reset was approved as low risk and completed from the knowledge document
    And the admin access request was held as high risk for a human approver
    And both requests have a trace in the audit trail for that run

  Scenario: Surge mode absorbs the queued requests during a P1
    Given 5 password reset tickets are waiting in the human queue
    When a P1 incident is declared
    Then all 5 queued requests are fulfilled by the agent
