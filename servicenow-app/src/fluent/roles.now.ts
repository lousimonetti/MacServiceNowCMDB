/**
 * The role granted to the integration user the connector authenticates as.
 *
 * The Identification and Reconciliation API itself requires `itil` or `asset`,
 * which must still be granted separately — a scoped role cannot confer those.
 * This role exists so that the integration account is identifiable in audit
 * trails and so instance-specific ACLs can be attached to one named grant
 * rather than to a bare user record.
 */
import { Role } from '@servicenow/sdk/core'

export const intuneSyncRole = Role({
    name: 'x_icsy_intune_cmdb.intune_sync',
    description:
        'Integration role for the intune-cmdb-sync connector. Grant to the ' +
        'OAuth Application User alongside itil (or asset) so IRE writes are permitted.',
    grantable: true,
    elevatedPrivilege: false,
})
