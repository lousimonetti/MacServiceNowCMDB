/**
 * OPTIONAL: adds `u_entra_object_id` to sys_user.
 *
 * Delete this file if you do not want the connector touching the global
 * `sys_user` dictionary — everything else works without it.
 *
 * Why it helps: matching an Intune device owner to a sys_user by email or
 * username breaks on mailbox migrations, domain renames, and duplicate
 * accounts. The Entra object ID never changes for the life of the account, so
 * populating this column (from your existing Entra-to-ServiceNow user feed) and
 * setting the connector to
 *
 *     SNOW_USER_MATCH_ORDER=entra_id,employee_number,email
 *     SNOW_USER_ENTRA_ID_FIELD=u_entra_object_id
 *
 * gives the most stable `assigned_to` linkage available.
 *
 * Note: creating a column on a global table from a scoped application requires
 * that the instance allows it (sys_db_object "Can create" access for cmdb/sys_user).
 * If `now-sdk install` rejects this record, create the column manually as an
 * admin in the global scope instead; the connector does not care how it got there.
 */
import { Record } from '@servicenow/sdk/core'

export const sysUserEntraObjectId = Record({
    $id: Now.ID['sys-user-entra-object-id'],
    table: 'sys_dictionary',
    data: {
        name: 'sys_user',
        element: 'u_entra_object_id',
        column_label: 'Entra object ID',
        internal_type: 'string',
        max_length: 40,
        active: true,
        display: false,
        read_only: false,
        comments:
            'Microsoft Entra ID directory object ID (GUID). Used by intune-cmdb-sync to ' +
            'resolve device owners to ServiceNow users.',
    },
})

/**
 * Index the new column: without it, every user lookup the connector performs is
 * a full table scan of sys_user.
 */
export const sysUserEntraObjectIdIndex = Record({
    $id: Now.ID['sys-user-entra-object-id-index'],
    table: 'sys_index',
    data: {
        table: 'sys_user',
        name: 'u_entra_object_id_idx',
        unique: false,
    },
})
