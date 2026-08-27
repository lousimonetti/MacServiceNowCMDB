/**
 * Registers "Intune" as a CMDB discovery source.
 *
 * This is not cosmetic and it is not optional. The Identification and
 * Reconciliation API validates `sysparm_data_source` against the choice list on
 * `cmdb_ci.discovery_source`; an unregistered value is rejected, so every write
 * from the connector fails until this choice exists.
 *
 * The value here must match the connector's SNOW_DISCOVERY_SOURCE environment
 * variable exactly, including case.
 */
import { Record } from '@servicenow/sdk/core'

export const intuneDiscoverySource = Record({
    $id: Now.ID['discovery-source-intune'],
    table: 'sys_choice',
    data: {
        name: 'cmdb_ci',
        element: 'discovery_source',
        value: 'Intune',
        label: 'Intune',
        language: 'en',
        sequence: 900,
        inactive: false,
    },
})
