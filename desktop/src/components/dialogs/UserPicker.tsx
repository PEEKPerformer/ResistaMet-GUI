// Who is measuring. Profiles live on the backend; this picks one or makes one.
// Opens on its own when no operator is selected, and cannot be dismissed
// until one is — every file the run writes is filed under the operator.

import { useEffect, useState } from "react";
import { useApi } from "../../app/AppContext";
import { setUsername, useUi } from "../../state/ui";
import { Button, Dialog, Input } from "../ui";
import { Icons } from "../icons";
import styles from "./dialogs.module.css";

interface Props {
  onClose: () => void;
}

export function UserPicker({ onClose }: Props) {
  const api = useApi();
  const ui = useUi();
  const [users, setUsers] = useState<string[] | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .users()
      .then(({ users, last_user }) => {
        setUsers(users);
        if (ui.username === null && last_user && users.includes(last_user)) {
          setUsername(last_user);
        }
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [api, ui.username]);

  const choose = async (name: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.addUser(name);
      setUsername(name);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const canDismiss = ui.username !== null;

  return (
    <Dialog
      title="Operator"
      onClose={canDismiss ? onClose : undefined}
      dismissable={canDismiss}
      footer={
        canDismiss ? (
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        ) : null
      }
    >
      {users === null && error === null ? <div className={styles.muted}>Loading…</div> : null}
      {users !== null ? (
        <ul className={styles.userList}>
          {users.map((name) => (
            <li key={name}>
              <button
                type="button"
                className={styles.userItem}
                data-selected={name === ui.username}
                disabled={busy}
                onClick={() => void choose(name)}
              >
                <Icons.user />
                <span>{name}</span>
                {name === ui.username ? <Icons.check size={14} /> : null}
              </button>
            </li>
          ))}
          {users.length === 0 ? <li className={styles.muted}>No profiles yet.</li> : null}
        </ul>
      ) : null}
      <form
        className={styles.newUser}
        onSubmit={(e) => {
          e.preventDefault();
          const name = draft.trim();
          if (name) void choose(name);
        }}
      >
        <Input
          value={draft}
          placeholder="New profile name"
          onChange={(e) => setDraft(e.target.value)}
          disabled={busy}
          autoFocus={users !== null && users.length === 0}
        />
        <Button type="submit" variant="primary" disabled={busy || draft.trim() === ""}>
          Add
        </Button>
      </form>
      {error ? <div className={styles.error}>{error}</div> : null}
    </Dialog>
  );
}
