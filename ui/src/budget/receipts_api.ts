// Receipts API view over the verified Budget gate in ./api so every receipt read and mutation
// inherits snapshot verification, expected-actor headers, lock checks, and stale-read aborts.
import { RECEIPT_CONTRACT_HEADERS, budgetApi } from "./api";
import type { BudgetApi } from "./api";
import type {
  ReceiptConfirmRequest,
  ReceiptDeleteRequest,
  ReceiptListRequest,
  ReceiptSearchRequest,
} from "./receipts_model";

export { RECEIPT_CONTRACT_HEADERS };

export type ReceiptsApi = Pick<
  BudgetApi,
  "receiptsList" | "receiptsSearch" | "receiptDetail" | "receiptImage" | "confirmReceipt" | "deleteReceipt"
> & Partial<Pick<
  BudgetApi,
  "fetchReceiptLinkGroupDetail" | "receiptGroupCandidates" | "linkReceiptGroup" | "unlinkReceiptGroup" | "isAuthorizationLocked"
>>;

export function createReceiptsApi(api: ReceiptsApi = budgetApi): ReceiptsApi {
  return {
    receiptsList: (request?: ReceiptListRequest) => api.receiptsList(request),
    receiptsSearch: (request: ReceiptSearchRequest) => api.receiptsSearch(request),
    receiptDetail: (receiptId: string) => api.receiptDetail(receiptId),
    ...(api.fetchReceiptLinkGroupDetail === undefined ? {}
      : { fetchReceiptLinkGroupDetail: api.fetchReceiptLinkGroupDetail.bind(api) }),
    ...(api.receiptGroupCandidates === undefined ? {}
      : { receiptGroupCandidates: api.receiptGroupCandidates.bind(api) }),
    ...(api.linkReceiptGroup === undefined ? {}
      : { linkReceiptGroup: api.linkReceiptGroup.bind(api) }),
    ...(api.unlinkReceiptGroup === undefined ? {}
      : { unlinkReceiptGroup: api.unlinkReceiptGroup.bind(api) }),
    receiptImage: (receiptId: string) => api.receiptImage(receiptId),
    confirmReceipt: (request: ReceiptConfirmRequest) => api.confirmReceipt(request),
    deleteReceipt: (request: ReceiptDeleteRequest) => api.deleteReceipt(request),
    ...(api.isAuthorizationLocked === undefined ? {} : { isAuthorizationLocked: () => api.isAuthorizationLocked?.() ?? false }),
  };
}

export const receiptsApi = createReceiptsApi();
