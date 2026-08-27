/**
 * Instance-side knobs. The connector reads none of these — they exist so a
 * CMDB administrator can see, from inside ServiceNow, what an inbound Intune
 * sync is expected to look like, and so business rules or reports can key off
 * the same source name the connector writes.
 */
import { Property } from '@servicenow/sdk/core'

export const discoverySourceProperty = Property({
    name: 'x_icsy_intune_cmdb.discovery_source',
    value: 'Intune',
    type: 'string',
    description:
        'Discovery source name written by the intune-cmdb-sync connector. Must match ' +
        'the connector SNOW_DISCOVERY_SOURCE variable and the sys_choice value.',
})

export const expectedRunIntervalProperty = Property({
    name: 'x_icsy_intune_cmdb.expected_run_interval_hours',
    value: '24',
    type: 'integer',
    description:
        'How often the external scheduler is expected to run the connector. Use this ' +
        'to alert when cmdb_ci.last_discovered for Intune-sourced CIs goes stale.',
})
