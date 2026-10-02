// Evaluate on a freshly navigated public product page. Read only JSON-LD.
(() => {
  const source = location.href;
  const url = new URL(source);
  if (!["www.galaxus.de", "www.alternate.de", "www.amazon.de", "www.otto.de",
        "www.cyberport.de", "www.notebooksbilliger.de", "www.coolblue.de", "www.expert.de"].includes(url.hostname) ||
      url.protocol !== "https:" || url.search || url.hash) {
    throw new Error("Navigate to a canonical supported product URL first");
  }
  const productFields = ["@type", "name", "url", "sku", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "brand"];
  const offerFields = ["@type", "url", "price", "priceCurrency", "itemCondition", "availability", "seller", "shippingDetails"];
  const pick = (value, fields) => Object.fromEntries(fields.filter(k => Object.hasOwn(value, k)).map(k => [k, value[k]]));
  const walk = value => {
    if (Array.isArray(value)) return value.flatMap(walk);
    if (!value || typeof value !== "object") return [];
    return [value, ...walk(value["@graph"]), ...walk(value.object)];
  };
  const products = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .flatMap(script => walk(JSON.parse(script.textContent)))
    .filter(product => ["Product", "ProductGroup"].includes(product["@type"]))
    .filter(product => {
      const offers = Array.isArray(product.offers) ? product.offers : [product.offers];
      return [product.url, ...offers.map(offer => offer?.url)].some(candidate => candidate === source);
    });
  if (products.length !== 1) throw new Error("Missing or ambiguous product JSON-LD");
  const original = products[0];
  const offers = Array.isArray(original.offers) ? original.offers : [original.offers];
  if (offers.some(offer => !offer || offer["@type"] !== "Offer")) throw new Error("Concrete offers required");
  const product = {...pick(original, productFields), offers: offers.map(offer => pick(offer, offerFields))};
  return {schema_version: 1, captures: [{source, observed_at: new Date().toISOString(), product}]};
})()
