(function () {
  'use strict';

  var DAY = 86400000;
  var fmt = new Intl.DateTimeFormat('fr-CH', { day: 'numeric', month: 'long', year: 'numeric' });

  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function get(obj, path) {
    return path.split('.').reduce(function (o, k) { return o == null ? o : o[k]; }, obj);
  }
  // Dates "AAAA-MM-JJ" en heure locale. La fin court jusqu'à 23:59:59.
  function parseDate(s, endOfDay) {
    var p = s.split('-').map(Number);
    return endOfDay ? new Date(p[0], p[1] - 1, p[2], 23, 59, 59) : new Date(p[0], p[1] - 1, p[2]);
  }

  fetch('/content/enquete.json', { cache: 'no-cache' })
    .then(function (r) { return r.json(); })
    .then(render)
    .catch(function () {
      $('h1').textContent = 'Le dossier n’a pas pu être chargé.';
    });

  function render(d) {
    document.title = d.titre + ' — Mise à l’enquête publique';

    document.querySelectorAll('[data-bind]').forEach(function (n) {
      var v = get(d, n.getAttribute('data-bind'));
      if (v != null) n.textContent = v;
    });

    renderStatus(d);
    renderProjet(d);
    renderDocuments(d.documents || []);
    renderMap(d.carte);
    renderParticipation(d);
    renderList('#etapes', d.etapes || [], function (e) {
      var li = el('li');
      li.appendChild(el('h3', null, e.titre));
      li.appendChild(el('p', null, e.texte));
      return li;
    });
    renderList('#faq-list', d.faq || [], function (f) {
      var det = el('details', 'faq');
      det.appendChild(el('summary', null, f.question));
      det.appendChild(el('p', null, f.reponse));
      return det;
    });

    var c = d.contact || {};
    var tel = $('#c-tel'); tel.textContent = c.telephone || ''; tel.href = 'tel:' + (c.telephone || '').replace(/\s/g, '');
    var mail = $('#c-mail'); mail.textContent = c.email || ''; mail.href = 'mailto:' + (c.email || '');
  }

  function renderList(sel, items, make) {
    var root = $(sel);
    items.forEach(function (i) { root.appendChild(make(i)); });
  }

  function renderStatus(d) {
    var start = parseDate(d.dateDebut), end = parseDate(d.dateFin, true), now = new Date();
    $('#date-debut').textContent = fmt.format(start);
    $('#date-fin').textContent = fmt.format(end);
    $('#warn-date').textContent = fmt.format(end);

    var box = $('#status'), badge = $('#status-badge'), big = $('#status-big'), small = $('#status-small');
    var pct;
    if (now < start) {
      var dd = Math.ceil((start - now) / DAY);
      box.dataset.state = 'avenir';
      badge.textContent = 'Enquête à venir';
      big.textContent = 'Dans ' + dd + (dd > 1 ? ' jours' : ' jour');
      small.textContent = 'Ouverture le ' + fmt.format(start);
      pct = 0;
    } else if (now <= end) {
      var left = Math.ceil((end - now) / DAY);
      box.dataset.state = 'ouverte';
      badge.textContent = 'Enquête en cours';
      big.textContent = left <= 1 ? 'Dernier jour' : left + ' jours restants';
      small.textContent = 'Remarques jusqu’au ' + fmt.format(end) + ' inclus';
      pct = Math.round(((now - start) / (end - start)) * 100);
    } else {
      box.dataset.state = 'close';
      badge.textContent = 'Enquête close';
      big.textContent = 'Délai échu';
      small.textContent = 'L’enquête s’est terminée le ' + fmt.format(end);
      pct = 100;
      $('#form-closed').hidden = false;
      $('#form').classList.add('is-closed');
      document.querySelectorAll('#form input, #form textarea, #form select, #form button').forEach(function (n) { n.disabled = true; });
    }
    $('#progress-bar').style.width = pct + '%';
    $('#progress').setAttribute('aria-valuenow', pct);
  }

  function renderProjet(d) {
    var desc = $('#description');
    (d.description || []).forEach(function (p) { desc.appendChild(el('p', null, p)); });
    renderList('#chiffres', d.chiffres || [], function (c) {
      var li = el('li');
      li.appendChild(el('strong', null, c.valeur));
      li.appendChild(el('span', null, c.libelle));
      return li;
    });
  }

  function renderDocuments(docs) {
    var root = $('#doc-groups');
    var groups = {};
    docs.forEach(function (doc) {
      var k = doc.categorie || 'Autres';
      (groups[k] = groups[k] || []).push(doc);
    });
    Object.keys(groups).forEach(function (cat) {
      var g = el('div', 'docgroup');
      g.appendChild(el('h3', null, cat));
      var ul = el('ul', 'docs');
      groups[cat].forEach(function (doc) {
        var li = el('li');
        li.dataset.search = (doc.titre + ' ' + cat).toLowerCase();
        var a = el('a', 'doc');
        a.href = doc.fichier;
        a.target = '_blank';
        a.rel = 'noopener';
        var ext = (doc.fichier.split('.').pop() || '').toUpperCase();
        a.appendChild(el('span', 'doc__icon', ext.slice(0, 4)));
        var t = el('span', 'doc__title', doc.titre);
        a.appendChild(t);
        a.appendChild(el('span', 'doc__meta', doc.taille ? doc.taille : 'Ouvrir'));
        li.appendChild(a);
        ul.appendChild(li);
      });
      g.appendChild(ul);
      root.appendChild(g);
    });

    $('#doc-search').addEventListener('input', function (e) {
      var q = e.target.value.trim().toLowerCase();
      root.querySelectorAll('.docgroup').forEach(function (g) {
        var any = false;
        g.querySelectorAll('li').forEach(function (li) {
          var show = !q || li.dataset.search.indexOf(q) !== -1;
          li.hidden = !show;
          any = any || show;
        });
        g.hidden = !any;
      });
    });
  }

  function renderMap(c) {
    if (!c) { $('#localisation').hidden = true; return; }
    var dLat = 0.0025, dLon = 0.004;
    var bbox = [c.lon - dLon, c.lat - dLat, c.lon + dLon, c.lat + dLat].join(',');
    $('#map').src = 'https://www.openstreetmap.org/export/embed.html?bbox=' + bbox + '&layer=mapnik&marker=' + c.lat + ',' + c.lon;
    $('#map-link').href = 'https://www.openstreetmap.org/?mlat=' + c.lat + '&mlon=' + c.lon + '#map=' + (c.zoom || 17) + '/' + c.lat + '/' + c.lon;
  }

  function renderParticipation(d) {
    var p = d.participation || {};
    renderList('#regles', p.regles || [], function (r) { return el('li', null, r); });
    $('#adresse-postale').textContent = p.adressePostale || '';
    $('#f-reference').value = d.reference || '';

    var form = $('#form');
    var warn = $('#warn-opposition');
    function syncType() {
      var t = form.querySelector('input[name="type"]:checked').value;
      warn.hidden = !(t === 'Opposition' && !p.depotEnLigneValable);
    }
    form.querySelectorAll('input[name="type"]').forEach(function (r) { r.addEventListener('change', syncType); });
    syncType();

    $('#print-letter').addEventListener('click', function () {
      if (!form.reportValidity()) return;
      buildLetter(d, new FormData(form));
      window.print();
    });
  }

  function buildLetter(d, f) {
    var L = $('#letter');
    L.textContent = '';
    var type = f.get('type');

    var from = el('div', 'letter__from');
    [f.get('prenom') + ' ' + f.get('nom'), f.get('adresse'), f.get('localite'), f.get('email')]
      .forEach(function (line) { from.appendChild(el('div', null, line)); });
    L.appendChild(from);

    var to = el('div', 'letter__to');
    to.textContent = (d.participation && d.participation.adressePostale) || d.commune;
    L.appendChild(to);

    L.appendChild(el('p', 'letter__date', (f.get('localite') || '').replace(/^\d{4}\s*/, '') + ', le ' + fmt.format(new Date())));

    var obj = el('p', 'letter__obj');
    obj.appendChild(el('strong', null, (type === 'Opposition' ? 'Opposition' : 'Observation') + ' — Enquête publique : ' + d.titre));
    L.appendChild(obj);
    L.appendChild(el('p', null, d.adresse + ' · ' + d.parcelle + ' · ' + d.reference));

    L.appendChild(el('p', null, 'Madame la Syndique, Monsieur le Syndic, Mesdames et Messieurs les Municipaux,'));
    L.appendChild(el('p', null, type === 'Opposition'
      ? 'Par la présente, je forme opposition au projet mentionné en objet, mis à l’enquête publique du ' + fmt.format(parseDate(d.dateDebut)) + ' au ' + fmt.format(parseDate(d.dateFin)) + ', pour les motifs suivants :'
      : 'Dans le cadre de l’enquête publique du projet mentionné en objet, je vous prie de prendre note des observations suivantes :'));

    var motifs = el('div', 'letter__body', f.get('motifs'));
    L.appendChild(motifs);

    if (type === 'Opposition') {
      L.appendChild(el('p', null, 'Je vous remercie de bien vouloir me tenir informé·e de la suite donnée à la présente opposition et de la décision de la Municipalité.'));
    }
    L.appendChild(el('p', null, 'Je vous prie d’agréer, Mesdames et Messieurs, l’expression de mes salutations distinguées.'));

    var sig = el('div', 'letter__sig');
    sig.appendChild(el('div', null, f.get('prenom') + ' ' + f.get('nom')));
    sig.appendChild(el('div', 'letter__line', 'Signature'));
    L.appendChild(sig);
  }
})();
