type OrderCorrelationDetailsProps = {
  smartupId?: string;
  skladbotRequestNumber?: string;
  skladbotRequestId?: string;
  returnRequestNumber?: string;
  returnRequestId?: string;
  showReturn?: boolean;
};

export default function OrderCorrelationDetails({
  smartupId,
  skladbotRequestNumber,
  skladbotRequestId,
  returnRequestNumber,
  returnRequestId,
  showReturn = false,
}: OrderCorrelationDetailsProps) {
  return (
    <div className="order-correlation">
      <span>Smartup ID: {canonicalSmartupIds(smartupId)}</span>
      <span>Заявка SkladBot: {requestReference(skladbotRequestNumber, skladbotRequestId)}</span>
      {showReturn && <span>Заявка возврата: {requestReference(returnRequestNumber, returnRequestId)}</span>}
    </div>
  );
}

function canonicalDecimalId(value: string | undefined, maxLength: number) {
  const normalized = String(value || "").trim();
  return new RegExp(`^[1-9][0-9]{0,${maxLength - 1}}$`).test(normalized) ? normalized : "—";
}

function canonicalSmartupIds(value: string | undefined) {
  const normalized = String(value || "").trim();
  if (!normalized) return "—";
  const ids = normalized.split(",").map((item) => item.trim());
  return ids.length > 0 && ids.every((item) => /^[1-9][0-9]{0,39}$/.test(item))
    ? ids.join(", ")
    : "—";
}

function requestReference(number: string | undefined, id: string | undefined) {
  const normalizedNumber = String(number || "").trim();
  if (
    normalizedNumber.length <= 80
    && /^(?:WH-R|WR)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$/.test(normalizedNumber)
  ) {
    return normalizedNumber;
  }
  const normalizedId = canonicalDecimalId(id, 20);
  return normalizedId === "—" ? normalizedId : `ID ${normalizedId}`;
}
