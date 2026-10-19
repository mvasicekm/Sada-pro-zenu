/* Kontrola kompatibility dílů mezi sebou a vůči konkrétnímu kolu.
 *
 * checkBuild(items, bike) vrací tři seznamy:
 *   hard   - takhle to nepojede, je potřeba jeden z dílů vyměnit,
 *   soft   - pravděpodobně problém, ale dá se obejít,
 *   verify - nevíme, protože to z inzerátu ani ze specifikace nejde vyčíst.
 *
 * ZÁSADA: konflikt se hlásí jen tehdy, když jsou oba porovnávané údaje
 * známé. Neznámý údaj nikdy nic nezakáže, jen spadne do "verify". Falešné
 * "nepasuje to" by stálo dobrou nabídku, falešné "pasuje" jen cestu na poštu.
 */
'use strict';

(function (root) {

  var PART_LABELS = {
    'lever': 'páky',
    'brake': 'brzdový třmen',
    'brake-lever': 'brzdové páky',
    'rear-derailleur': 'přehazovačka',
    'front-derailleur': 'přesmykač',
    'crank': 'kliky',
    'cassette': 'kazeta',
    'chain': 'řetěz'
  };

  // Díly, u kterých se počet rychlostí musí shodovat přesně.
  var SPEED_CRITICAL = ['lever', 'rear-derailleur', 'cassette', 'chain'];
  // Tady je neshoda spíš varování - kliky i přesmykač často přesahují řady.
  var SPEED_SOFT = ['crank', 'front-derailleur'];

  function partLabel(item) {
    var part = item.specs && item.specs.part;
    return PART_LABELS[part] || (item.category_label || 'díl');
  }

  function shortTitle(item) {
    var title = item.title || '';
    return title.length > 44 ? title.slice(0, 43) + '\u2026' : title;
  }

  function withPart(items, parts) {
    return items.filter(function (item) {
      return item.specs && parts.indexOf(item.specs.part) !== -1;
    });
  }

  /** Seskupí díly podle hodnoty parametru, neznámé vynechá. */
  function groupByKnown(items, key) {
    var groups = {};
    items.forEach(function (item) {
      var value = item.specs ? item.specs[key] : null;
      if (value === null || value === undefined) { return; }
      if (!groups[value]) { groups[value] = []; }
      groups[value].push(item);
    });
    return groups;
  }

  function describeGroups(groups) {
    return Object.keys(groups).map(function (value) {
      var names = groups[value].map(function (item) {
        return partLabel(item) + ' (' + shortTitle(item) + ')';
      });
      return value + ': ' + names.join(', ');
    }).join(' \u2014 ');
  }

  function checkSpeeds(items, out) {
    var critical = withPart(items, SPEED_CRITICAL);
    var groups = groupByKnown(critical, 'speeds');
    var values = Object.keys(groups);
    if (values.length > 1) {
      out.hard.push({
        title: 'Nesedí počet rychlostí',
        detail: describeGroups(groups)
          + '. Páky, přehazovačka, kazeta a řetěz musí mít stejný počet.'
      });
    }

    var known = values.length === 1 ? Number(values[0]) : null;
    if (known === null) { return null; }

    var softGroups = groupByKnown(withPart(items, SPEED_SOFT), 'speeds');
    Object.keys(softGroups).forEach(function (value) {
      if (Number(value) === known) { return; }
      out.soft.push({
        title: 'Jiná řada u ' + softGroups[value].map(partLabel).join(', '),
        detail: 'Zbytek sady je ' + known + '\u00d7, tohle ' + value
          + '\u00d7. Kliky i přesmykač často fungují napříč řadami, ale ověř si to.'
      });
    });
    return known;
  }

  function checkFront(items, out) {
    var levers = withPart(items, ['lever']);
    var cranks = withPart(items, ['crank']);
    var groups = groupByKnown(levers.concat(cranks), 'front');
    if (Object.keys(groups).length > 1) {
      out.hard.push({
        title: 'Nesedí počet převodníků vpředu',
        detail: describeGroups(groups)
          + '. Páky pro 2\u00d7 nepřehodí jednopřevodník a naopak.'
      });
    }

    var hasFd = withPart(items, ['front-derailleur']).length > 0;
    var onlySingle = Object.keys(groups).length === 1 && Number(Object.keys(groups)[0]) === 1;
    if (hasFd && onlySingle) {
      out.hard.push({
        title: 'Přesmykač v jednopřevodníkové sadě',
        detail: 'Sada je postavená jako 1\u00d7, přesmykač v ní nemá co dělat.'
      });
    }
  }

  function checkBrakes(items, bike, out) {
    var isDiscFrame = String(bike.brake_mount || '').indexOf('disc') !== -1;

    withPart(items, ['brake']).forEach(function (item) {
      if (isDiscFrame && item.specs.brake === 'rim') {
        out.hard.push({
          title: 'Ráfkový třmen na kotoučový rám',
          detail: shortTitle(item) + ' \u2013 rám i vidlice mají '
            + (bike.brake_mount || 'kotoučové') + ', ráfkovou brzdu není kam přišroubovat.'
        });
      }
    });

    // Hydraulika a lanko se neovládají navzájem.
    var levers = withPart(items, ['lever', 'brake-lever']);
    var calipers = withPart(items, ['brake']);
    var leverHydraulic = levers.some(function (i) { return i.specs.brake === 'disc-hydraulic'; });
    var leverCable = levers.some(function (i) {
      return i.specs.brake === 'rim' || i.specs.brake === 'disc-mechanical';
    });
    var caliperHydraulic = calipers.some(function (i) { return i.specs.brake === 'disc-hydraulic'; });
    var caliperCable = calipers.some(function (i) {
      return i.specs.brake === 'rim' || i.specs.brake === 'disc-mechanical';
    });

    if (leverHydraulic && caliperCable) {
      out.hard.push({
        title: 'Hydraulické páky k lankovému třmenu',
        detail: 'Hydraulická páka nemá lanko, kterým by třmen utáhla.'
      });
    }
    if (leverCable && caliperHydraulic) {
      out.hard.push({
        title: 'Lankové páky k hydraulickému třmenu',
        detail: 'Hydraulický třmen potřebuje hydraulickou páku.'
      });
    }

    if (bike.target_brake === 'disc-hydraulic' && leverHydraulic && !calipers.length) {
      out.verify.push({
        title: 'K hydraulickým pákám dokup třmeny',
        detail: 'Kolo má zatím lankové Tektro. Hydraulické páky je neovládnou,'
          + ' počítej do rozpočtu i třmeny a odvzdušnění.'
      });
    }
    if (calipers.length && bike.rotor_mm) {
      out.verify.push({
        title: 'Adaptér na kotouč ' + bike.rotor_mm + ' mm',
        detail: 'Flat mount třmeny jsou z výroby na 140 mm, na '
          + bike.rotor_mm + ' mm je potřeba adaptér.'
      });
    }
  }

  function checkElectronic(items, out) {
    var shifting = withPart(items, ['lever', 'rear-derailleur', 'front-derailleur']);
    var electronic = shifting.filter(function (i) { return i.specs.electronic; });
    if (electronic.length && electronic.length !== shifting.length) {
      out.hard.push({
        title: 'Di2 a mechanika dohromady',
        detail: 'Elektronické řazení (' + electronic.map(partLabel).join(', ')
          + ') neovládne mechanické díly. Celé řazení musí být stejné.'
      });
    }
  }

  function checkCassette(items, bike, out, buildSpeeds) {
    var cassettes = withPart(items, ['cassette']);
    if (!cassettes.length) { return; }

    var speeds = buildSpeeds;
    cassettes.forEach(function (item) {
      if (item.specs.speeds) { speeds = item.specs.speeds; }
    });

    if (speeds && speeds >= 11) {
      if (bike.freehub && bike.freehub !== '11' && bike.freehub !== '11s') {
        out.hard.push({
          title: 'Kazeta se na náboj nevejde',
          detail: speeds + 'rychlostní kazeta potřebuje delší (11s) tělo náboje,'
            + ' tvoje je ' + bike.freehub + '.'
        });
      } else if (!bike.freehub) {
        out.verify.push({
          title: 'Ověř tělo náboje (ořech)',
          detail: speeds + 'rychlostní kazeta potřebuje 11s tělo náboje. Kolo je'
            + ' z výroby 9rychlostní, takže tohle rozhoduje o tom, jestli stačí'
            + ' koupit kazetu, nebo i nový náboj. Zjisti to dřív, než cokoliv koupíš.'
        });
      }
    }

    // Největší pastorek musí zvládnout klec přehazovačky.
    var limits = bike.rd_max_cog || {};
    withPart(items, ['rear-derailleur']).forEach(function (rd) {
      var max = rd.specs.model ? limits[rd.specs.model] : null;
      if (!max) { return; }
      cassettes.forEach(function (cassette) {
        if (!cassette.specs.cog_max) { return; }
        if (cassette.specs.cog_max > max) {
          out.hard.push({
            title: 'Kazeta je na přehazovačku moc velká',
            detail: 'Kazeta má ' + cassette.specs.cog_max + ' zubů, ale '
              + (rd.specs.model || '').toUpperCase() + ' zvládne nejvýš ' + max
              + '. Řetěz by dřel o klec nebo by se přehazovačka otírala o paprsky.'
          });
        }
      });
      if (!cassettes.some(function (c) { return c.specs.cog_max; })) {
        out.verify.push({
          title: 'Zjisti rozsah kazety',
          detail: (rd.specs.model || '').toUpperCase() + ' utáhne nejvýš ' + max
            + ' zubů. V inzerátu rozsah není uvedený.'
        });
      }
    });
  }

  function checkChainrings(items, bike, out) {
    var cranks = withPart(items, ['crank']);
    if (!cranks.length) { return; }

    var capacity = bike.fd_max_capacity;
    var hasFd = withPart(items, ['front-derailleur']).length > 0;
    cranks.forEach(function (crank) {
      var big = crank.specs.chainring_big;
      var small = crank.specs.chainring_small;
      if (hasFd && capacity && big && small && (big - small) > capacity) {
        out.hard.push({
          title: 'Moc velký rozdíl převodníků',
          detail: big + '/' + small + ' je rozdíl ' + (big - small)
            + ' zubů, přesmykač zvládne ' + capacity + '.'
        });
      }
      if (!big) {
        out.verify.push({
          title: 'Zjisti velikost převodníků',
          detail: shortTitle(crank) + ' \u2013 v inzerátu nejsou zuby uvedené.'
            + ' Teď máš ' + (bike.current_chainrings || []).join('/') + '.'
        });
      }
    });

    if (!bike.bb_shell) {
      out.verify.push({
        title: 'Ověř středové složení',
        detail: 'Shimano kliky (Hollowtech II) potřebují odpovídající střed;'
          + ' kolo má z výroby FSA cartridge. Zjisti, jestli je rám BSA se'
          + ' závitem, nebo PressFit.'
      });
    }
    if (!bike.frame_size) {
      out.verify.push({
        title: 'Zjisti velikost rámu kvůli délce klik',
        detail: 'Původní kliky jsou podle velikosti 165\u2013172,5 mm.'
          + ' Koupit o dost delší je nepohodlné.'
      });
    }
  }

  function checkFdMount(items, bike, out) {
    if (!withPart(items, ['front-derailleur']).length) { return; }
    if (!bike.fd_mount) {
      out.verify.push({
        title: 'Braze-on, nebo objímka?',
        detail: 'Přesmykač se dělá v obou verzích a nejsou zaměnitelné.'
          + ' Podívej se, jestli má rám na sedlové trubce přišroubovaný čep.'
      });
    }
  }

  /** Hlavní kontrola. items = pole záznamů z data.json, bike = profil kola. */
  function checkBuild(items, bike) {
    var out = { hard: [], soft: [], verify: [] };
    bike = bike || {};
    items = items || [];
    if (!items.length) { return out; }

    var speeds = checkSpeeds(items, out);
    checkFront(items, out);
    checkBrakes(items, bike, out);
    checkElectronic(items, out);
    checkCassette(items, bike, out, speeds);
    checkChainrings(items, bike, out);
    checkFdMount(items, bike, out);
    return out;
  }

  root.Compat = { checkBuild: checkBuild, PART_LABELS: PART_LABELS };

}(typeof module !== 'undefined' && module.exports ? module.exports : window));
