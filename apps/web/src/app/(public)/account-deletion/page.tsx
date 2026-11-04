import { AccountDeletionStatus } from '@/modules/account';

/** Render public progress after account session revocation; the API checks the receipt cookie. */
export default function AccountDeletionPage() {
  return <AccountDeletionStatus />;
}
