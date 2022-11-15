from collections import defaultdict
import logging

from seqr.models import Sample
from seqr.utils.elasticsearch.utils import InvalidSearchException
from seqr.utils.elasticsearch.constants import RECESSIVE, COMPOUND_HET, NEW_SV_FIELD
from seqr.utils.elasticsearch.es_search import EsSearch
from seqr.utils.search_backend.hail_query_wrapper import QUERY_CLASS_MAP, STRUCTURAL_ANNOTATION_FIELD, \
    AllVariantHailTableQuery, AllSvHailTableQuery, AllDataTypeHailTableQuery

logger = logging.getLogger(__name__)

SV_ANNOTATION_TYPES = {'structural_consequence', STRUCTURAL_ANNOTATION_FIELD, NEW_SV_FIELD}


class HailSearch(object):

    def __init__(self, families, previous_search_results=None, return_all_queried_families=False, user=None, sort=None):
        self.samples = Sample.objects.filter(
            is_active=True, individual__family__in=families,
        ).select_related('individual__family', 'individual__family__project')

        projects = {s.individual.family.project for s in self.samples}
        genome_version_projects = defaultdict(list)
        for p in projects:
            genome_version_projects[p.get_genome_version_display()].append(p.name)
        if len(genome_version_projects) > 1:
            project_builds = '; '.join(f'{build} [{", ".join(projects)}]' for build, projects in genome_version_projects.items())
            raise InvalidSearchException(
                f'Search is only enabled on a single genome build, requested the following project builds: {project_builds}')
        self._genome_version = list(genome_version_projects.keys())[0]

        self._user = user
        self._sort = sort
        self._return_all_queried_families = return_all_queried_families # In production: need to implement for reloading saved variants
        self.previous_search_results = previous_search_results or {}

    def _load_table(self, data_type, **kwargs):
        sample_data_sources_by_type = defaultdict(lambda: defaultdict(list))
        for s in self.samples:
            data_type_key = f'{s.dataset_type}_{s.sample_type}' if s.dataset_type == Sample.DATASET_TYPE_SV_CALLS else s.dataset_type
            sample_data_sources_by_type[data_type_key][s.elasticsearch_index].append(s)  # In production: should use a different model field, not elasticsearch_index
        multi_data_sources = next(
            (data_sources for data_sources in sample_data_sources_by_type.values() if len(data_sources) > 1), None)
        if multi_data_sources:
            raise InvalidSearchException(
                f'Search is only enabled on a single data source, requested {", ".join(multi_data_sources.keys())}')
        data_sources_by_type = {k: list(v.keys())[0] for k, v in sample_data_sources_by_type.items()}
        samples_by_data_type = {k: list(v.values())[0] for k, v in sample_data_sources_by_type.items()}

        if data_type == Sample.DATASET_TYPE_VARIANT_CALLS:
            data_sources_by_type = {
                k: v for k, v in data_sources_by_type.items()
                if k in {Sample.DATASET_TYPE_VARIANT_CALLS, Sample.DATASET_TYPE_MITO_CALLS}
            }
        elif data_type == Sample.DATASET_TYPE_SV_CALLS:
            data_sources_by_type = {k: v for k, v in data_sources_by_type.items() if k.startswith(Sample.DATASET_TYPE_SV_CALLS)}
            samples_by_data_type = {k: v for k, v in samples_by_data_type.items() if k.startswith(Sample.DATASET_TYPE_SV_CALLS)}

        single_data_type = list(data_sources_by_type.keys())[0] if len(data_sources_by_type) == 1 else None

        if single_data_type:
            samples = samples_by_data_type[single_data_type]
            data_source = data_sources_by_type[single_data_type]
            query_cls = QUERY_CLASS_MAP[single_data_type]
        else:
            samples = samples_by_data_type
            data_source = data_sources_by_type
            is_all_svs = all(k.startswith(Sample.DATASET_TYPE_SV_CALLS) for k in data_sources_by_type)
            is_no_sv = all(not k.startswith(Sample.DATASET_TYPE_SV_CALLS) for k in data_sources_by_type)

            if is_all_svs:
                query_cls = AllSvHailTableQuery
            elif is_no_sv:
                query_cls = AllVariantHailTableQuery
            else:
                query_cls = AllDataTypeHailTableQuery

        self._query_wrapper = query_cls(data_source, samples=samples, genome_version=self._genome_version, **kwargs)

    @classmethod
    def process_previous_results(cls, previous_search_results, page=1, num_results=100, load_all=False):
        # return EsSearch.process_previous_results(*args, **kwargs)
        # TODO #2496: re-enable caching, not helpful for initial development
        return None, {'page': page, 'num_results': num_results}

    def filter_variants(self, inheritance=None, genes=None, intervals=None, variant_ids=None, locus=None,
                        annotations=None, annotations_secondary=None, quality_filter=None, skip_genotype_filter=False,
                        **kwargs):
        has_location_filter = genes or intervals

        if variant_ids:
            # In production: support SV variant IDs?
            variant_ids = [EsSearch.parse_variant_id(variant_id) for variant_id in variant_ids]
            intervals = [f'[{chrom}:{pos}-{pos}]' for chrom, pos, _, _ in variant_ids]
            data_type = Sample.DATASET_TYPE_VARIANT_CALLS
        else:
            data_type = self._dataset_type_for_annotations(annotations, annotations_secondary) if annotations else None

        genes = genes or {}
        parsed_intervals = None
        exclude_locations = (locus or {}).get('excludeLocations')
        if has_location_filter:
            gene_coords = [
                {field: gene[f'{field}{self._genome_version.title()}'] for field in ['chrom', 'start', 'end']}
                for gene in genes.values()
            ]
            parsed_intervals = ['{chrom}:{start}-{end}'.format(**interval) for interval in intervals or []] + [
                '{chrom}:{start}-{end}'.format(**gene) for gene in gene_coords]

        self._load_table(
            data_type, intervals=parsed_intervals, exclude_intervals=exclude_locations,
            gene_ids=None if exclude_locations else set(genes.keys()))

        if variant_ids:
            self._query_wrapper.filter_by_variant_ids(variant_ids)

        quality_filter = quality_filter or {}
        self._query_wrapper.filter_variants(annotations=annotations, quality_filter=quality_filter, **kwargs)

        inheritance_mode = (inheritance or {}).get('mode')
        inheritance_filter = (inheritance or {}).get('filter') or {}
        if inheritance_filter.get('genotype'):
            inheritance_mode = None
        if not inheritance_mode and inheritance_filter and list(inheritance_filter.keys()) == ['affected']:
            raise InvalidSearchException('Inheritance must be specified if custom affected status is set')

        if inheritance_mode in {RECESSIVE, COMPOUND_HET}:
            comp_het_only = inheritance_mode == COMPOUND_HET
            self._query_wrapper.filter_compound_hets(
                inheritance_filter, annotations_secondary, quality_filter, has_location_filter, keep_main_ht=not comp_het_only,
            )
            if comp_het_only:
                return

        self._query_wrapper.filter_main_annotations()
        self._query_wrapper.annotate_filtered_genotypes(inheritance_mode, inheritance_filter, quality_filter)

    @staticmethod
    def _dataset_type_for_annotations(annotations, annotations_secondary):
        annotation_types = {k for k, v in annotations.items() if v}
        if annotations_secondary:
            annotation_types.update({k for k, v in annotations_secondary.items() if v})

        if NEW_SV_FIELD in annotation_types or annotation_types.issubset(SV_ANNOTATION_TYPES):
            return Sample.DATASET_TYPE_SV_CALLS
        elif annotation_types.isdisjoint(SV_ANNOTATION_TYPES):
            return Sample.DATASET_TYPE_VARIANT_CALLS
        return None

    def filter_by_variant_ids(self, variant_ids):
        self.filter_variants(variant_ids=variant_ids)

    def search(self, page=1, num_results=100):
        hail_results, total_results = self._query_wrapper.search(page, num_results, self._sort)
        self.previous_search_results['total_results'] = total_results
        # TODO #2496 actually cache results
        return hail_results
