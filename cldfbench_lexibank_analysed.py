import itertools
import pathlib
import zipfile
import textwrap
import collections

import pycldf
from cldfbench import CLDFSpec
from cldfbench import Dataset as BaseDataset
from clldutils.misc import nfilter
from clldutils.path import readlines
from cltoolkit import Wordlist
from cltoolkit.features import FeatureCollection, feature_data
from pyclts import CLTS
from git import Repo, GitCommandError
from tqdm import tqdm
from csvw.metadata import Link

CLTS_2_1 = (
    "https://zenodo.org/record/4705149/files/cldf-clts/clts-v2.1.0.zip?download=1",
    'cldf-clts-clts-04f04e3')


class Dataset(BaseDataset):
    dir = pathlib.Path(__file__).parent
    id = "lexibank-analysed"

    def cldf_specs(self):
        return {
            'phonology': CLDFSpec(
                metadata_fname='phonology-metadata.json',
                data_fnames=dict(
                    ParameterTable='phonology-features.csv',
                    ValueTable='phonology-values.csv',
                    CodeTable='phonology-codes.csv',
                ),
                dir=self.cldf_dir, module="StructureDataset"),
            'lexicon': CLDFSpec(
                metadata_fname='lexicon-metadata.json',
                data_fnames=dict(
                    ParameterTable='lexicon-features.csv',
                    ValueTable='lexicon-values.csv',
                    CodeTable='lexicon-codes.csv',
                ),
                dir=self.cldf_dir, module="StructureDataset"),
            'phonemes': CLDFSpec(
                metadata_fname='phonemes-metadata.json',
                data_fnames=dict(
                    ParameterTable='phonemes.csv',
                    ValueTable='frequencies.csv',
                ),
                dir=self.cldf_dir, module="StructureDataset"),
        }

    def cmd_download(self, args):
        for row in self.etc_dir.read_csv('lexibank.tsv', delimiter='\t', dicts=True):
            args.log.info("Checking {}".format(row["Dataset"]))
            dest = self.raw_dir / row["Dataset"]
            if not row["LexiCore"].strip() and not row["ClicsCore"].strip():
                args.log.info("... skipping dataset.")
            elif dest.exists():
                args.log.info("... dataset already exists.")
            else:
                args.log.info("... cloning {}".format(row["Dataset"]))
                try:
                    Repo.clone_from(
                        "https://github.com/{}/{}.git".format(row["Organization"], row["Dataset"]),
                        str(dest),
                    )
                except GitCommandError as e:
                    args.log.error("... download failed\n{}".format(str(e)))

        with self.raw_dir.temp_download(CLTS_2_1[0], 'ds.zip', log=args.log) as zipp:
            zipfile.ZipFile(str(zipp)).extractall(self.raw_dir)

    def load_data(self, set_):
        """
        Load all datasets from a defined group of datasets.
        """
        return [
            pycldf.Dataset.from_metadata(self.raw_dir / ds / "cldf" / "cldf-metadata.json")
            for ds in nfilter(readlines(self.etc_dir / '{}.txt'.format(set_), strip=True))]

    def cmd_makecldf(self, args):
        languages = collections.OrderedDict()
        fc = FeatureCollection.from_data(feature_data())

        def _add_features(writer, features):
            for feature in features:
                writer.objects['ParameterTable'].append(dict(
                    ID=feature.id,
                    Name=feature.name,
                    Description=textwrap.dedent(
                        feature.function.__doc__ if not feature.function.__doc__.strip().startswith('partial') else ''),
                ))
                if feature.categories:
                    for k, v in feature.categories.items():
                        writer.objects['CodeTable'].append(dict(
                            Parameter_ID=feature.id,
                            ID='{}-{}'.format(feature.id, k),
                            Name=v,
                        ))

        def _add_language(writer, language, features, attr_features):
            l = {
                "ID": language.id,
                "Name": language.name,
                "Glottocode": language.glottocode,
                "Dataset": language.dataset,
                "Latitude": language.latitude,
                "Longitude": language.longitude,
                "Subgroup": language.subgroup,
                "Family": language.family,
            }
            languages[language.id] = l
            writer.objects['LanguageTable'].append(l)
            for attr in attr_features:
                writer.objects['ValueTable'].append(dict(
                    ID='{}-{}'.format(language.id, attr),
                    Language_ID=language.id,
                    Parameter_ID=attr,
                    Value=len(getattr(language, attr))
                ))
            for feature in features:
                v = feature(language)
                cid = str(v).lower()
                cid = "null" if cid == "none" else cid
                if feature.categories:
                    assert cid in feature.categories, '{}: "{}"'.format(feature.id, cid)
                writer.objects['ValueTable'].append(dict(
                    ID='{}-{}'.format(language.id, feature.id),
                    Language_ID=language.id,
                    Parameter_ID=feature.id,
                    Value=v,
                    Code_ID='{}-{}'.format(feature.id, cid) if feature.categories else None,
                ))

        def _add_languages(writer, wordlist, condition, features, attr_features):
            for language in tqdm(wordlist.languages, desc='computing features'):
                if language.name == None or language.name == "None":
                    args.log.warning('{0.dataset}: {0.id}: {0.name}'.format(language))
                    continue
                if language.latitude and condition(language):
                    _add_language(writer, language, features, attr_features)
                    yield language

        with self.cldf_writer(args, cldf_spec='phonology') as writer:
            # FIXME: work around cldfbench bug (can't rename core table of a module!):
            writer.cldf['ValueTable'].url = Link('phonology-values.csv')
            writer.cldf.add_component('LanguageTable', 'Dataset', 'Subgroup', 'Family')

            features = [f for f in fc.features if f.module.endswith("phonology")]

            for fid, fname, fdesc in [
                ('concepts', 'Number of concepts', 'Number of senses linked to Concepticon'),
                ('forms', 'Number of forms', ''),
                ('bipa_forms', 'Number of BIPA conforming forms', ''),
                ('senses', 'Number of senses', ''),
            ]:
                writer.objects['ParameterTable'].append(
                    dict(ID=fid, Name=fname, Description=fdesc))
            _add_features(writer, features)

            sounds = collections.defaultdict(collections.Counter)
            for language in _add_languages(
                writer,
                Wordlist(datasets=self.load_data('lexicore'), ts=CLTS(self.raw_dir / CLTS_2_1[1]).bipa),
                lambda l: len(l.bipa_forms) >= 80,
                features,
                ['concepts', 'forms', 'bipa_forms', 'senses'],
            ):
                for sound in language.sound_inventory.segments:
                    sounds[(sound.obj.name.replace(' ', '_'), sound.obj.s)][language.id] = len(sound.occs)

        with self.cldf_writer(args, cldf_spec='lexicon', clean=False) as writer:
            # FIXME: work around cldfbench bug (can't rename core table of a module!):
            writer.cldf['ValueTable'].url = Link('lexicon-values.csv')
            writer.cldf.add_component('LanguageTable', 'Dataset', 'Subgroup', 'Family')
            features = [f for f in fc.features if f.module.endswith("lexicon")]

            for fid, fname, fdesc in [
                ('concepts', 'Number of concepts', 'Number of senses linked to Concepticon'),
                ('forms', 'Number of forms', ''),
                ('senses', 'Number of senses', ''),
            ]:
                writer.objects['ParameterTable'].append(
                    dict(ID=fid, Name=fname, Description=fdesc))
            _add_features(writer, features)
            _ = list(_add_languages(
                writer,
                Wordlist(datasets=self.load_data('clics')),
                lambda l: len(l.concepts) >= 250,
                features,
                ['concepts', 'forms', 'senses']))

        with self.cldf_writer(args, cldf_spec='phonemes', clean=False) as writer:
            #
            # FIXME: work around cldfbench bug (can't rename core table of a module!):
            writer.cldf['ValueTable'].url = Link('frequencies.csv')
            #
            writer.cldf.add_columns('ParameterTable', 'cltsReference')
            writer.cldf.add_component('LanguageTable', 'Dataset', 'Subgroup', 'Family')
            writer.objects['LanguageTable'] = languages.values()
            for clts_id, glyphs in itertools.groupby(sorted(sounds.keys()), lambda k: k[0]):
                glyphs = [g[1] for g in glyphs]
                occs = collections.Counter()
                for glyph in glyphs:
                    occs.update(**sounds[clts_id, glyph])

                writer.objects['ParameterTable'].append(dict(
                    ID=clts_id,
                    Name=' / '.join(glyphs),
                    CLTS_ID=clts_id,
                ))
                for lid, freq in occs.items():
                    writer.objects['ValueTable'].append(dict(
                        ID='{}-{}'.format(lid, clts_id),
                        Language_ID=lid,
                        Parameter_ID=clts_id,
                        Value=freq,
                    ))
