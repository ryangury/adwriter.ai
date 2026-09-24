/* Shared card renderers for the Ad Writer page and the Inventory detail page.
   esc/fmtMiles/fmtPrice/fmtNum/daysChip/cardRecon/cardCarfax/cardProofPoints/
   cardMarketData are moved verbatim out of templates/index.html; cardSticker
   and cardEmpty are new. Exposed as window.Cards. */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtMiles(n) {
    if (n == null || n === "") return "—";
    var v = Number(n);
    return isNaN(v) ? String(n) : v.toLocaleString("en-US") + " mi";
  }

  function fmtPrice(n) {
    if (n == null || n === "") return "—";
    var v = Number(n);
    return isNaN(v) ? String(n) : "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  function fmtNum(n) {
    if (n == null || n === "") return "—";
    var v = Number(n);
    return isNaN(v) ? String(n) : v.toLocaleString("en-US");
  }

  function daysBucketClass(days) {
    if (days == null || days === "") return null;
    var v = Number(days);
    if (isNaN(v)) return null;
    if (v <= 40) return "days-green";
    if (v <= 59) return "days-yellow";
    if (v <= 89) return "days-orange";
    return "days-red";
  }

  function daysChip(days) {
    var cls = daysBucketClass(days);
    if (!cls) return "";
    return '<span class="dchip ' + cls + '">' + esc(days) + " days</span>";
  }

  function cardRecon(included, excluded) {
    included = included || [];
    excluded = excluded || [];
    if (!included.length && !excluded.length) {
      return '<div class="dcard"><h3>Recon</h3><p class="dempty">No qualifying recon items</p></div>';
    }
    var incHtml = included.map(function (li) {
      return '<div class="dline d-ok"><span class="glyph">&#10003;</span>' +
        '<span class="dtext">' + esc(li.description || li.reason || "—") + "</span></div>";
    }).join("") || '<p class="dempty">None</p>';
    var excHtml = excluded.map(function (li) {
      var text = esc(li.description || "—") + (li.rule ? " &mdash; " + esc(li.rule) : "");
      return '<div class="dline d-skip"><span class="glyph">&#10007;</span>' +
        '<span class="dtext">' + text + "</span></div>";
    }).join("") || '<p class="dempty">None</p>';
    return '<div class="dcard"><h3>Recon</h3>' +
      '<div class="dsub">Included</div>' + incHtml +
      '<div class="dsub">Excluded</div>' + excHtml +
      "</div>";
  }

  function cardCarfax(d) {
    if (!d) return "";
    var owners = d.number_of_owners != null ?
      esc(d.number_of_owners) + (d.owner_type ? " (" + esc(d.owner_type) + ")" : "") : "—";
    var rows = '<div class="dline"><span class="dtext">Owners: ' + owners + "</span></div>";
    var clean = d.no_accidents === true;
    var known = d.no_accidents === true || d.no_accidents === false;
    rows += '<div class="dline ' + (clean ? "d-ok" : known ? "d-warn" : "d-skip") + '"><span class="glyph">' +
      (clean ? "&#10003;" : known ? "&#9888;" : "?") + '</span><span class="dtext">' +
      (clean ? "No accidents reported" : known ?
        "Accident reported" + (d.accident_details ? " &mdash; " + esc(typeof d.accident_details === "string" ? d.accident_details : JSON.stringify(d.accident_details)) : "") :
        "Accident status unknown") + "</span></div>";
    var brands = d.title_brands;
    var hasBrands = Array.isArray(brands) ? brands.length > 0 : !!brands;
    if (hasBrands) {
      rows += '<div class="dline d-warn"><span class="glyph">&#9888;</span><span class="dtext">Title brands: ' +
        esc(Array.isArray(brands) ? brands.join(", ") : brands) + "</span></div>";
    }
    if (d.carfax_date) {
      rows += '<div class="dline"><span class="dtext">Report date: ' + esc(d.carfax_date) + "</span></div>";
    }
    if (d.image_available && d.vin) {
      var url = "/carfax-image/" + encodeURIComponent(d.vin);
      rows += '<div class="dline"><a href="' + url + '" target="_blank" rel="noopener">' +
        '<img src="' + url + '" alt="Cached Carfax report" style="max-width:100%;max-height:160px;border:1px solid #ccc"></a></div>';
    }
    return '<div class="dcard"><h3>Carfax</h3>' + rows + "</div>";
  }

  function cardProofPoints(points) {
    points = points || [];
    if (!points.length) {
      return '<div class="dcard"><h3>Proof points</h3><p class="dempty">No proof points evaluated</p></div>';
    }
    var lines = points.map(function (p) {
      var selected = p.selected === "primary" || p.selected === "secondary";
      var favorable = p.direction === "below";
      var cls = selected ? "d-ok" : favorable ? "d-skip" : "d-warn";
      var glyph = selected ? "&#10003;" : favorable ? "~" : "&#10007;";
      var gapText = p.gap != null ? fmtPrice(p.gap) + " " + (p.direction || "") : "";
      var tag = p.selected === "primary" ? '<span class="dtag">Primary</span>' :
        p.selected === "secondary" ? '<span class="dtag">Secondary</span>' : "";
      var extra = !selected && p.skip_reason ?
        ' <span class="dmuted">(' + esc(p.skip_reason) + ")</span>" : "";
      var text = esc(p.label || "—") + (gapText ? " &mdash; " + esc(gapText) : "") + extra;
      return '<div class="dline ' + cls + '"><span class="glyph">' + glyph + "</span>" +
        '<span class="dtext">' + text + "</span>" + tag + "</div>";
    }).join("");
    return '<div class="dcard"><h3>Proof points</h3>' + lines + "</div>";
  }

  function cardMarketData(md) {
    if (!md) return "";
    var shipOk = !!md.shipping_triggered;
    var rows =
      '<div class="dline"><span class="dtext">Matching listings: ' +
        esc(fmtNum(md.matching_count)) + "</span></div>" +
      '<div class="dline"><span class="dtext">Overall days supply: ' +
        esc(fmtNum(md.overall_days_supply)) + "</span>" + daysChip(md.overall_days_supply) + "</div>" +
      '<div class="dline"><span class="dtext">Matching days supply: ' +
        esc(fmtNum(md.market_days_supply)) + "</span>" + daysChip(md.market_days_supply) + "</div>" +
      '<div class="dline"><span class="dtext">Avg mileage in market: ' +
        esc(fmtNum(md.avg_mileage)) + "</span></div>" +
      '<div class="dline"><span class="dtext">Price rank: ' + esc(md.price_rank || "—") + "</span></div>" +
      '<div class="dline"><span class="dtext">Search radius: ' +
        (md.search_distance != null ?
          esc((md.market_scope || "—") + " (" + fmtNum(md.search_distance) + " miles)") :
          "—") + "</span></div>" +
      '<div class="dline ' + (shipOk ? "d-ok" : "d-skip") + '"><span class="glyph">' +
        (shipOk ? "&#10003;" : "&#10007;") + "</span><span class=\"dtext\">Shipping" +
        (shipOk && md.shipping_reason ? " &mdash; " + esc(md.shipping_reason) : "") + "</span></div>";
    return '<div class="dcard"><h3>Market data</h3>' + rows + "</div>";
  }

  // ---- new in the shared file (no equivalent existed in index.html) ----

  function cardEmpty(title, message) {
    return '<div class="dcard"><h3>' + esc(title) + '</h3><p class="dempty">' + esc(message) + "</p></div>";
  }

  // Window sticker summary: totals + priced packages. `s` is the shape built
  // by app.py's _sticker_card() from the cached AutoiPacket / Carfax / ACV Max
  // sticker data.
  function cardSticker(s) {
    if (!s) return cardEmpty("Window sticker", "No window sticker cached");
    var rows = "";
    if (s.source) {
      rows += '<div class="dline"><span class="dtext dmuted">Source: ' + esc(s.source) + "</span></div>";
    }
    rows += '<div class="dline"><span class="dtext">Total MSRP: ' +
      esc(s.total_msrp != null ? fmtPrice(s.total_msrp) : "not on file") + "</span></div>";
    if (s.base_price != null) {
      rows += '<div class="dline"><span class="dtext">Base price: ' + esc(fmtPrice(s.base_price)) + "</span></div>";
    }
    if (s.freight != null) {
      rows += '<div class="dline"><span class="dtext">Freight: ' + esc(fmtPrice(s.freight)) + "</span></div>";
    }
    var pk = s.packages || [];
    rows += '<div class="dsub">Option packages (' + pk.length + ")</div>";
    if (!pk.length) {
      rows += '<p class="dempty">None priced on the sticker</p>';
    } else {
      rows += pk.map(function (p) {
        var row = '<div class="dline d-ok"><span class="glyph">&#10003;</span><span class="dtext">' +
          esc((p.code ? p.code + " — " : "") + (p.name || "—")) + "</span>" +
          '<span class="dtext dprice">' + esc(p.price != null ? fmtPrice(p.price) : "") + "</span></div>";
        // What the package includes, as printed under it on the sticker.
        if (p.contents && p.contents.length) {
          row += '<ul class="sticker-contents">' +
            p.contents.map(function (c) {
              return "<li>" + esc(typeof c === "string" ? c :
                (c.name || JSON.stringify(c))) + "</li>";
            }).join("") + "</ul>";
        }
        return row;
      }).join("");
    }
    if (s.standard_count) {
      rows += '<div class="dline"><span class="dtext dmuted">' + esc(s.standard_count) + " standard items listed</span></div>";
    }
    return '<div class="dcard"><h3>Window sticker</h3>' + rows + "</div>";
  }

  global.Cards = {
    esc: esc, fmtMiles: fmtMiles, fmtPrice: fmtPrice, fmtNum: fmtNum,
    daysBucketClass: daysBucketClass, daysChip: daysChip,
    cardRecon: cardRecon, cardCarfax: cardCarfax, cardProofPoints: cardProofPoints,
    cardMarketData: cardMarketData, cardSticker: cardSticker, cardEmpty: cardEmpty
  };
})(window);
