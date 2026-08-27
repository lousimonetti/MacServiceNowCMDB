/**
 * Intune CMDB Sync — ServiceNow-side configuration.
 *
 * This application ships no runtime logic. The sync itself runs outside the
 * instance (Azure Container Apps or AWS Lambda) and writes through the
 * base-platform Identification and Reconciliation API, so there is nothing here
 * that needs a subscription or a Service Graph Connector entitlement.
 *
 * What it does provide is the platform configuration the connector depends on,
 * captured as code so it is versioned, reviewable, and reproducible across
 * dev/test/prod instances instead of being clicked in by hand.
 */
export * from './discovery-source.now'
export * from './roles.now'
export * from './properties.now'
export * from './sys-user-entra-id.now'
