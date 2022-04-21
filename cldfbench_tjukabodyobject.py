import pathlib
import collections

import pycldf
from cldfbench import CLDFSpec
from cldfbench import Dataset as BaseDataset
from cltoolkit import Wordlist
from cltoolkit.features import FeatureCollection, Feature
from cltoolkit.features.lexicon import Colexification
from cldfzenodo import oai_lexibank
from git import Repo, GitCommandError
from tqdm import tqdm
from csvw.dsv import reader
from csvw.utils import slug

COLLECTIONS = {
    'ClicsCore': (
        'Wordlists with large form inventories in which at least 250 concepts can be linked to '
        'the Concepticon',
        'large wordlists with at least 250 concepts'),
}
CONDITIONS = {
    "ClicsCore": lambda x: len(x.concepts) >= 250,
}


class Dataset(BaseDataset):
    dir = pathlib.Path(__file__).parent
    id = "tjukabodyobject"

    def cldf_specs(self):
        return CLDFSpec(
            metadata_fname='lexicon-metadata.json',
            data_fnames=dict(
                ParameterTable='lexicon-features.csv',
                ValueTable='lexicon-values.csv',
                CodeTable='lexicon-codes.csv',
            ),
            dir=self.cldf_dir, module="StructureDataset")

    @property
    def dataset_meta(self):
        try:
            return self._dataset_meta
        except AttributeError:
            dataset_meta = collections.OrderedDict()
            for row in self.etc_dir.read_csv('lexibank.csv', delimiter=',', dicts=True):
                if not row['Zenodo'].strip():
                    continue
                row['collections'] = set(key for key in COLLECTIONS if row.get(key, '').strip() == 'x')
                if 'ClicsCore' in row['collections']:
                    dataset_meta[row['Dataset']] = row
            self._dataset_meta = dataset_meta
            return self._dataset_meta

    def cmd_download(self, args):
        github_info = {rec.doi: rec.github_repos for rec in oai_lexibank()}

        for dataset, row in self.dataset_meta.items():
            ghinfo = github_info[row['Zenodo']]
            args.log.info("Checking {}".format(dataset))
            dest = self.raw_dir / dataset

            # download data
            if dest.exists():
                args.log.info("... dataset already exists.  pulling changes.")
                for remote in Repo(str(dest)).remotes:
                    remote.fetch()
            else:
                args.log.info("... cloning {}".format(dataset))
                try:
                    Repo.clone_from(ghinfo.clone_url, str(dest))
                except GitCommandError as e:
                    args.log.error("... download failed\n{}".format(str(e)))
                    continue

            # check out release (fall back to master branch)
            repo = Repo(str(dest))
            if ghinfo.tag:
                args.log.info('... checking out tag {}'.format(ghinfo.tag))
                repo.git.checkout(ghinfo.tag)
            else:
                args.log.warning('... could not determine tag to check out')
                args.log.info('... checking out master')
                try:
                    branch = repo.branches.main
                    branch.checkout()
                except AttributeError:
                    try:
                        branch = repo.branches.master
                        branch.checkout()
                    except AttributeError:
                        args.log.error('found neither main nor master branch')
                repo.git.merge()

    def _datasets(self, set_=None, with_metadata=False):
        """
        Load all datasets from a defined group of datasets.
        """
        if set_:
            dataset_ids = [
                dataset_id
                for dataset_id, md in self.dataset_meta.items()
                if set_ in md['collections']]
        else:
            dataset_ids = list(self.dataset_meta)

        # avoid duplicates
        dataset_ids = sorted(set(dataset_ids))

        for dataset_id in dataset_ids:
            dataset = pycldf.Dataset.from_metadata(
                self.raw_dir / dataset_id / "cldf" / "cldf-metadata.json")
            metadata = self.dataset_meta[dataset_id]
            yield (dataset, metadata) if with_metadata else dataset

    def _schema(self, writer):
        writer.cldf.add_component(
            'LanguageTable',
            {
                'name': 'Dataset',
                'propertyUrl': 'http://cldf.clld.org/v1.0/terms.rdf#contributionReference',
            },
            {'name': 'Forms', 'datatype': 'integer', 'dc:description': 'Number of forms'},
            {'name': "FormsWithSounds", "datatype": "integer",
                "dc:description": "Number of forms with sounds"},
            {'name': 'Concepts', 'datatype': 'integer', 'dc:description': 'Number of concepts'},
            {'name': 'Incollections'},
            'Subgroup',
            'Family')
        t = writer.cldf.add_table(
            'collections.csv',
            'ID',
            'Name',
            'Description',
            'Varieties',
            'Glottocodes',
            'Concepts',
            'Forms',
        )
        t.tableSchema.primaryKey = ['ID']
        writer.cldf.add_component(
            'ContributionTable',
            {'name': 'Collection_IDs', 'separator': ' '},
            'Glottocodes',
            'Doculects',
            'Concepts',
            'Senses',
            'Forms',
        )
        writer.cldf.add_foreign_key('ContributionTable', 'Collection_IDs', 'collections.csv', 'ID')

    def cmd_makecldf(self, args):
        concept_list = self.etc_dir.read_csv(
            'Tjuka-2022-784.tsv', dicts=True, delimiter='\t')
        bodyparts = [
            row['CONCEPTICON_GLOSS']
            for row in concept_list
            if row['GROUP'] == 'body']
        objects = [
            row['CONCEPTICON_GLOSS']
            for row in concept_list
            if row['GROUP'] == 'object']
        features = FeatureCollection(
            Feature(
                id='{}And{}'.format(
                    slug(bodypart).capitalize(),
                    slug(obj).capitalize()),
                name="colexification of {} and {}".format(bodypart, obj),
                function=Colexification(bodypart, obj))
            for bodypart in bodyparts
            for obj in objects)

        languages = collections.OrderedDict()
        values = []

        features_found = set()

        condition = CONDITIONS["ClicsCore"]  # lambda l: len(l.concepts) >= 250
        collection = 'ClicsCore'
        attr_features = ['concepts', 'forms', 'senses']

        with self.cldf_writer(args) as writer:
            self._schema(writer)
            writer.cldf.add_columns(
                'ParameterTable',
                {"name": "Feature_Spec", "datatype": "json"},
            )

            # XXX: doe we actually need the `concepts`, `forms`, and `senses` params?
            for fid, fname, fdesc in [
                ('concepts', 'Number of concepts', 'Number of senses linked to Concepticon'),
                ('forms', 'Number of forms', ''),
                ('senses', 'Number of senses', ''),
            ]:
                writer.objects['ParameterTable'].append(
                    dict(ID=fid, Name=fname, Description=fdesc))

            for dataset in self._datasets('ClicsCore'):
                wordlist = Wordlist(datasets=[dataset])
                for language in tqdm(wordlist.languages, desc='computing features'):
                    if language.name is None or language.name == "None":
                        args.log.warning('{0.dataset}: {0.id}: {0.name}'.format(language))
                        continue
                    if not language.latitude or not condition(language):
                        continue
                    l = languages.get(language.id)
                    if not l:
                        l = {
                            "ID": language.id,
                            "Name": language.name,
                            "Glottocode": language.glottocode,
                            "Dataset": language.dataset,
                            "Latitude": language.latitude,
                            "Longitude": language.longitude,
                            "Subgroup": language.subgroup,
                            "Family": language.family,
                            "Forms": len(language.forms or []),
                            "FormsWithSounds": len(language.forms_with_sounds or []),
                            "Concepts": len(language.concepts),
                            "Incollections": collection,
                        }
                    else:
                        l['Incollections'] = l['Incollections'] + collection
                    languages[language.id] = l
                    for attr in attr_features:
                        values.append(dict(
                            ID='{}-{}'.format(language.id, attr),
                            Language_ID=language.id,
                            Parameter_ID=attr,
                            Value=len(getattr(language, attr))
                        ))
                    for feature in features:
                        v = feature(language)
                        if not v:
                            continue
                        features_found.add(feature.id)
                        if feature.categories:
                            assert v in feature.categories, '{}: "{}"'.format(feature.id, v)
                        values.append(dict(
                            ID='{}-{}'.format(language.id, feature.id),
                            Language_ID=language.id,
                            Parameter_ID=feature.id,
                            Value=v,
                            Code_ID='{}-{}'.format(feature.id, v) if feature.categories else None,
                        ))

                        # yield language

            writer.objects['LanguageTable'] = languages.values()
            writer.objects['ValueTable'] = values

            for feature in features:
                if feature.id not in features_found:
                    continue
                writer.objects['ParameterTable'].append(dict(
                    ID=feature.id,
                    Name=feature.name,
                    Description=feature.doc,
                    Feature_Spec=feature.to_json(),
                ))
                if feature.categories:
                    for k, v in feature.categories.items():
                        writer.objects['CodeTable'].append(dict(
                            Parameter_ID=feature.id,
                            ID='{}-{}'.format(feature.id, k),
                            Name=v,
                        ))
